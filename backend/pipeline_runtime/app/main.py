"""Pipeline execution runtime entrypoint - the loop that closes the gap
`backend/tenant_api/app/api/pipeline_assignments.py`'s own docstring names honestly:
"This records intent, not execution." Everything downstream of a detection (rules,
incidents, evidence, notifications) already worked; nothing pulled a camera's stream and
ran the assigned pipeline against it until this service existed.

**One `asyncio.Task` per active cloud camera, not a shared poll loop** (the plan's own
"Decisions made before any code": cameras have independent, unrelated cadences, so a
shared scheduler buys nothing and a per-camera task is simpler and correct). A short
discovery poll (`run_discovery_loop`) is the only thing that ever looks at every camera at
once - it starts a task for a newly-active assignment, and stops one whose assignment was
revoked or itself changed (so a tenant's tenant_overrides edit, or a pipeline being
deprecated out from under a running camera, takes effect within one discovery interval
too, not only a revoke).

**Two things carried forward from Task 2's own code-quality review** (see
`csense_shared.pipeline.runtime.run_one_cycle`'s own docstring for the full context),
both specifically about this module:

1. `run_one_cycle`'s `grab_frame_fn` calls a plain synchronous callable, unawaited - never
   `async def`. `_sync_frame_closure` below is that trivial closure; it does no I/O of its
   own, because the frame is already fetched by the time it is built.
2. `grab_frame` (`csense_shared.cameras.frame_grab`) is a **blocking** call with up to a
   10s timeout. `_grab_frame_for_assignment` dispatches it to the default executor
   (`loop.run_in_executor`) rather than calling it inline, so one unreachable camera's
   timeout never stalls every other camera's due cycle on this same event loop.

**Discovery scheduling (`run_discovery_loop`) takes its dependencies as injected
callables** (`fetch_assignments`, `build_run_cycle`) precisely so the per-camera
`asyncio.Task` spawn/cancel/isolation mechanics - the actual subject of this task's own
spec - are testable with real asyncio, no real database, camera, or ai-runtime call
needed (`backend/tests/test_pipeline_runtime_service.py`). `amain` below is the only place
that wires the real `active_cloud_assignments`/`_run_one_camera_cycle` in.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import signal
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import cv2
import httpx
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from csense_shared.cameras.connection import resolve_camera_endpoint
from csense_shared.cameras.frame_grab import grab_frame
from csense_shared.config import Settings, get_settings
from csense_shared.db.postgres import create_engine, create_session_factory, platform_session, tenant_session
from csense_shared.logging import configure_logging, get_logger
from csense_shared.pipeline.ingest import ingest_detection
from csense_shared.pipeline.rules import DetectedObject
from csense_shared.pipeline.runtime import Assignment, active_cloud_assignments, run_one_cycle
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import BlockedAddressError
from csense_shared.security.secret_store import read_secret
from csense_shared.storage.objects import create_client

logger = get_logger(__name__)

# Matches `backend/tenant_api/app/api/cameras.py`'s own `CREDENTIAL_PURPOSE` constant for
# the exact same stored secret - each service that decrypts a camera credential defines
# its own copy rather than sharing one across a tenant_api-only module and this
# independently-deployed service, mirroring the duplication that already exists between
# `cameras.py` and `media.py` for the same reason.
CREDENTIAL_PURPOSE = "camera.rtsp"

_CAMERA_SECRET_QUERY = text("SELECT endpoint_secret_id FROM cameras WHERE id = :id")

RunCycle = Callable[[Assignment, dt.datetime], Awaitable[str]]


# --- Pure scheduling helpers -----------------------------------------------------------


def interval_seconds(sample_fps: float) -> float:
    """How long one camera's task sleeps between cycles. `sample_fps` is always positive
    (`merge_resource_profile`'s own defaults/validation guarantee this before an
    `Assignment` ever reaches here), but a defensive floor keeps a malformed value from
    turning into a zero-second busy loop against ai-runtime and the database.
    """
    if sample_fps <= 0:
        return 1.0
    return 1.0 / sample_fps


def rotate_for_admission(
    candidate_camera_ids: list[uuid.UUID], admission_round: int
) -> list[uuid.UUID]:
    """Decides which not-yet-running candidate is considered first for an open capacity
    slot this discovery cycle, given `run_discovery_loop`'s own hard cap on concurrently-
    running camera tasks (`pipeline_runtime_max_concurrent_cameras`).

    Without this, "who gets the next open slot" would always be whatever order
    `current.items()` happens to iterate in - itself just `fetch_assignments()`'s own
    returned order, which real Postgres query plans tend to keep stable poll over poll.
    At a saturated cap, that would mean the same tail of candidates loses the race for a
    freed slot every single time a slot opens, cycle after cycle, purely as an accident of
    dict/list ordering rather than any real priority. Rotating the starting offset by one
    each discovery cycle (`admission_round`, incremented once per poll) spreads "first in
    line for the next open slot" across every waiting candidate over time instead.

    **What this does NOT do, deliberately**: it never touches an already-*running* task.
    `run_discovery_loop` only ever calls this on the subset of `current` that is not
    already in `tasks`, so rotation can change who is offered the *next* open slot, but it
    can never preempt a slot that is already filled - doing that would be exactly the
    "cancel and respawn something already running just because a new candidate showed up"
    thrashing this module's own docstring already rules out. So this closes the "same
    candidates always lose" bias for *waiting* cameras; it does not, and should not,
    prevent a fully saturated fleet with no revokes for a long time from continuing to run
    whichever cameras happened to be admitted first - that is inherent to "no thrashing,"
    not a bug in this rotation.
    """
    if not candidate_camera_ids:
        return []
    offset = admission_round % len(candidate_camera_ids)
    return candidate_camera_ids[offset:] + candidate_camera_ids[:offset]


def sync_frame_closure(frame: np.ndarray | None) -> Callable[[Assignment], np.ndarray | None]:
    """The trivial synchronous closure `run_one_cycle` gets as `grab_frame_fn`.

    Must never be `async def`: `run_one_cycle` calls it unawaited
    (`frame = grab_frame_fn(...)`), so a coroutine function here would silently hand it a
    coroutine object - never `None` - instead of a real frame or a real absence of one,
    defeating the `if frame is None: return "unreachable"` check entirely (Task 2
    code-review warning #1).
    """

    def _fn(_assignment: Assignment) -> np.ndarray | None:
        return frame

    return _fn


# --- Production frame acquisition: async resolve+decrypt, then executor-dispatched grab --


async def _decrypt_camera_password(session, settings: Settings, assignment: Assignment) -> str | None:
    """Mirrors `camera_probe.py`'s own `_probe_stream` credential-decrypt sequence
    (`camera_secret_query` -> `keyring_from_settings` -> `read_secret`) rather than
    reinventing it - this service just cannot import that tenant_api-only module, so the
    one-line lookup query is duplicated the same way it already is between `cameras.py`
    and `media.py`.
    """
    if not assignment.username:
        return None
    secret_id = (
        await session.execute(_CAMERA_SECRET_QUERY, {"id": assignment.camera_id})
    ).scalar_one_or_none()
    if secret_id is None:
        return None
    keyring = keyring_from_settings(settings)
    return (
        await read_secret(
            session, keyring,
            secret_id=secret_id, tenant_id=assignment.tenant_id, purpose=CREDENTIAL_PURPOSE,
        )
    ).decode()


async def rtsp_url_for_assignment(
    assignment: Assignment, *, settings: Settings, session_factory: async_sessionmaker,
) -> str | None:
    """Resolves the camera's dial-safe address, decrypts its credential fresh, and builds
    the `rtsp://` URL `grab_frame` will dial - the async half of the frame-grab seam.

    Returns `None` - never raises - for a camera whose endpoint this deployment refuses to
    reach (`BlockedAddressError`) or whose credential cannot be decrypted right now
    (`EnvelopeError`/`UnicodeDecodeError`): both are exactly the "camera didn't answer"
    condition `camera_probe.py`'s own convention already treats as information for the
    operator, not a crash for this loop's own per-camera isolation to absorb.

    Runs inside a tenant-scoped session for this camera's own tenant - the connectivity
    allowlist (`tunnel_networks`) and the credential's AAD are both tenant-bound, and RLS
    is the correct, already-established way to keep one tenant's camera from resolving
    against another tenant's tunnel provisioning.
    """
    if not assignment.hostname or not assignment.rtsp_port or not assignment.main_stream_path:
        logger.info(
            "camera_missing_stream_details", extra={"camera_id": str(assignment.camera_id)}
        )
        return None

    try:
        async with tenant_session(session_factory, assignment.tenant_id) as session:
            address, port = await resolve_camera_endpoint(
                session,
                camera_id=assignment.camera_id,
                hostname=assignment.hostname,
                port=assignment.rtsp_port,
            )
            password = await _decrypt_camera_password(session, settings, assignment)
    except BlockedAddressError as exc:
        logger.info(
            "camera_endpoint_blocked",
            extra={"camera_id": str(assignment.camera_id), "error": str(exc)[:200]},
        )
        return None
    except (EnvelopeError, UnicodeDecodeError) as exc:
        logger.warning(
            "camera_credential_unreadable",
            extra={"camera_id": str(assignment.camera_id), "error": str(exc)[:200]},
        )
        return None

    auth = ""
    if assignment.username and password:
        # RTSP userinfo is a URL component - matches `camera_stream.py`'s own
        # `resolve_camera_rtsp_url` quoting, for the same reason (a password containing
        # `@`, `:` or `/` would otherwise be parsed as part of the host or path).
        auth = f"{quote(assignment.username, safe='')}:{quote(password, safe='')}@"
    return f"rtsp://{auth}{address}:{port}{assignment.main_stream_path}"


async def grab_frame_for_assignment(
    assignment: Assignment,
    *,
    settings: Settings,
    session_factory: async_sessionmaker,
    grab_frame_fn: Callable[..., np.ndarray | None] = grab_frame,
    resolve_url_fn: Callable[..., Awaitable[str | None]] = rtsp_url_for_assignment,
) -> np.ndarray | None:
    """Task 3's own frame-acquisition seam: the async DB-bound chain (resolve + decrypt +
    URL build, all awaited) runs first, then the **blocking** `grab_frame_fn` call is
    dispatched to the default executor - never inline - so one stalled camera's up-to-10s
    timeout never stalls every other camera's due cycle on this same event loop (Task 2
    code-review warning #2, carried forward verbatim into this task's own spec).

    `grab_frame_fn`/`resolve_url_fn` are injectable (defaulting to the real
    `grab_frame`/`rtsp_url_for_assignment`) so `backend/tests/test_pipeline_runtime_service.py`
    can prove the real executor-dispatch mechanics - that this event loop keeps making
    progress on other work while a fake blocking call sleeps - without a real camera or
    database.
    """
    url = await resolve_url_fn(assignment, settings=settings, session_factory=session_factory)
    if url is None:
        return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(
            grab_frame_fn, url, timeout_seconds=settings.pipeline_runtime_frame_grab_timeout_seconds
        ),
    )


# --- Production ai-runtime call ---------------------------------------------------------


async def infer_via_ai_runtime(
    http_client: httpx.AsyncClient,
    model_name: str,
    frame: np.ndarray,
    *,
    confidence: float,
) -> list[DetectedObject]:
    """Calls the real ai-runtime service's `/internal/v1/infer` (`backend/ai_runtime/app/
    main.py`) with plain `httpx`, matching how `notification_worker`/`webhook_dispatch.py`
    calls out to its own internal collaborators - a multipart POST, an explicit timeout, no
    retry (a missed cycle is picked up again next cycle, the same "crash loses nothing"
    posture `webhook_dispatch.py`'s own `run_once` already uses).

    The frame is a decoded BGR `numpy` array (`grab_frame`'s own return type); ai-runtime's
    `/infer` takes an uploaded image file, so it is re-encoded as JPEG here rather than
    asking ai-runtime to accept raw arrays - JPEG is the one image encoding every HTTP
    client/server pair in this codebase already speaks.
    """
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        logger.warning("frame_encode_failed", extra={"model_name": model_name})
        return []

    response = await http_client.post(
        "/internal/v1/infer",
        data={"model_name": model_name, "confidence": str(confidence)},
        files={"frame": ("frame.jpg", encoded.tobytes(), "image/jpeg")},
    )
    response.raise_for_status()
    payload = response.json()
    return [
        DetectedObject(
            class_name=d["class_name"],
            confidence=float(d["confidence"]),
            bbox=tuple(d["bbox"]),
        )
        for d in payload.get("detections", [])
    ]


# --- Production per-camera cycle: wires grab/infer/ingest into run_one_cycle -----------


async def run_one_camera_cycle(
    assignment: Assignment,
    now: dt.datetime,
    *,
    settings: Settings,
    session_factory: async_sessionmaker,
    http_client: httpx.AsyncClient,
    object_store,
) -> str:
    """The real `RunCycle` a running camera task actually calls each pass: grabs a frame
    (off the event loop), calls ai-runtime, and - through `csense_shared.pipeline.runtime.
    run_one_cycle` - ingests any qualifying detection in-process via `ingest_detection`,
    inside a `tenant_session` scoped to this camera's own tenant. Per the plan's own
    "Decisions" section: the discovery read is platform-scoped, but every write stays
    tenant-scoped - no detection is ever written cross-tenant.
    """
    frame = await grab_frame_for_assignment(
        assignment, settings=settings, session_factory=session_factory
    )

    async def _http_infer_fn(model_name: str, image: np.ndarray, *, confidence: float) -> list[DetectedObject]:
        return await infer_via_ai_runtime(http_client, model_name, image, confidence=confidence)

    async def _ingest_fn(**kwargs):
        async with tenant_session(session_factory, assignment.tenant_id) as session:
            return await ingest_detection(session, object_store, **kwargs)

    return await run_one_cycle(
        assignment,
        grab_frame_fn=sync_frame_closure(frame),
        http_infer_fn=_http_infer_fn,
        ingest_fn=_ingest_fn,
        now=now,
    )


# --- Per-camera task lifecycle: the actual subject of this task's own spec -------------


async def _camera_loop(assignment: Assignment, *, run_cycle: RunCycle, stop: asyncio.Event) -> None:
    """One camera's own polling loop: cycle, sleep to its own `sample_fps`-derived
    interval, repeat, until `stop` is set or this task is cancelled (a revoked/replaced
    assignment - see `run_discovery_loop`).

    A per-cycle exception is logged and swallowed rather than left to end the task - the
    same "one poisonous delivery must not stop the pass" posture
    `webhook_dispatch.py`'s own `run_once` already established, just at the granularity of
    one camera's one cycle instead of one queued delivery. `asyncio.CancelledError` is the
    one exception that must not be swallowed here: it is how the discovery loop actually
    stops this task, and eating it would make a revoke never take effect.
    """
    camera_id = assignment.camera_id
    interval = interval_seconds(assignment.sample_fps)
    logger.info(
        "camera_task_started",
        extra={"camera_id": str(camera_id), "sample_fps": assignment.sample_fps, "interval_seconds": interval},
    )
    try:
        while not stop.is_set():
            started = dt.datetime.now(dt.UTC)
            try:
                status = await run_cycle(assignment, started)
                logger.debug(
                    "camera_cycle_completed", extra={"camera_id": str(camera_id), "status": status}
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see this function's own docstring
                logger.exception("camera_cycle_failed", extra={"camera_id": str(camera_id)})

            elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(0.05, interval - elapsed))
    finally:
        logger.info("camera_task_stopped", extra={"camera_id": str(camera_id)})


async def _isolated_camera_loop(assignment: Assignment, *, run_cycle: RunCycle, stop: asyncio.Event) -> None:
    """Runs one camera's own loop so that its death is never another camera's death, nor
    the discovery loop's own - the same *shape* as `notification_worker/app/main.py`'s
    `_webhook_dispatch_never_takes_alerts_down_with_it`, adapted here for "one camera's
    task" instead of "one loop-type's task."

    `_camera_loop` already swallows per-cycle exceptions internally (mirroring
    `run_forever`'s own per-pass guard in `notification_worker/app/worker.py`), so reaching
    this handler means something escaped that inner guard entirely - a real bug in this
    module's own setup, not an ordinary offline-camera condition. Logged and swallowed
    rather than left to propagate into `run_discovery_loop`'s own `await task` on
    cancellation/replacement, or become an "exception was never retrieved" surprise raised
    from an un-awaited task days later: one misbehaving camera's task must never take any
    other camera, or camera discovery itself, down with it.
    """
    try:
        await _camera_loop(assignment, run_cycle=run_cycle, stop=stop)
    except asyncio.CancelledError:
        raise  # Shutdown or revocation, not failure - must propagate or the task cannot stop.
    except Exception:  # noqa: BLE001 - see this function's own docstring
        logger.exception(
            "camera_task_died_other_cameras_continue", extra={"camera_id": str(assignment.camera_id)}
        )


async def _stop_camera_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def run_discovery_loop(
    *,
    fetch_assignments: Callable[[], Awaitable[list[Assignment]]],
    build_run_cycle: Callable[[Assignment], RunCycle],
    discovery_interval_seconds: float,
    max_concurrent_cameras: int,
    stop: asyncio.Event,
) -> None:
    """Spawns or continues one `asyncio.Task` per active cloud camera, on a short poll -
    up to `max_concurrent_cameras` at once (`pipeline_runtime_max_concurrent_cameras`; see
    that setting's own comment in `csense_shared.config` for exactly where the default of
    160 comes from and its real, flat-count-not-weighted-budget limitation).

    `fetch_assignments()` -> the current `list[Assignment]` (production: `amain` wires in
    `active_cloud_assignments` under a `platform_session`, matching that function's own
    "dispatch legitimately spans every tenant" scoping). `build_run_cycle(assignment)` ->
    the `RunCycle` that camera's own task calls every pass (production: `amain` wires in
    `run_one_camera_cycle`, closed over `settings`/`session_factory`/`http_client`/
    `object_store`). Both are injected so this function - the actual per-camera task
    spawn/cancel/isolation logic this task's spec is about - is fully testable with real
    asyncio and no real database, camera, or ai-runtime call
    (`backend/tests/test_pipeline_runtime_service.py`).

    A camera whose current assignment differs at all from what its running task was given
    - not only a revoke, but a `tenant_overrides` edit or the pipeline itself moving to
    `deprecated` - gets its task cancelled and a fresh one spawned with the new
    `Assignment`, within one discovery interval. `Assignment` is a frozen dataclass, so
    `!=` is a real field-by-field comparison, not identity - this is what makes "a newly-
    assigned camera gets picked up within one discovery-poll interval" and "a revoked
    assignment's task is cancelled cleanly" the same code path rather than two.

    **Admission control**: a newly-discovered active assignment only gets a task spawned
    for it while the number of already-running tasks is below `max_concurrent_cameras`.
    At capacity, it is turned away - logged as `camera_admission_refused_capacity` (at the
    same INFO level, and the same "logged again next cycle for as long as it keeps being
    true" cadence, as this module's own `camera_endpoint_blocked` for an unreachable
    camera) so an operator can see *why* a camera silently isn't running rather than
    guessing, matching this service's own "capacity refusal is a health fact, not a
    crash" posture. This never touches an already-running task - nothing here cancels a
    running camera to make room for a new one - so recomputing "who's running vs. who's
    active" every cycle is also what makes a freed slot (a revoke, a deprecated pipeline,
    a changed assignment) pick up a previously-turned-away assignment on the very next
    discovery cycle, with no special-case code for it: it falls out of the same "spawn
    whatever's active and not already running, up to the cap" pass. `rotate_for_admission`
    (see its own docstring) is what keeps that pass from always favoring the same waiting
    candidates over others when the cap is saturated.

    One poll failing (a transient DB hiccup) leaves every already-running camera task
    exactly as it was - it does not tear anything down - and is logged, matching this
    service's own "an unreachable camera/database is a health fact, not a crash" stance.
    """
    tasks: dict[uuid.UUID, asyncio.Task] = {}
    live_assignments: dict[uuid.UUID, Assignment] = {}
    admission_round = 0

    try:
        while not stop.is_set():
            try:
                assignments = await fetch_assignments()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see this function's own docstring
                logger.exception("assignment_discovery_poll_failed")
                assignments = None

            if assignments is not None:
                current = {a.camera_id: a for a in assignments}

                for camera_id in list(tasks):
                    if current.get(camera_id) != live_assignments.get(camera_id):
                        await _stop_camera_task(tasks.pop(camera_id))
                        live_assignments.pop(camera_id, None)

                candidates = [camera_id for camera_id in current if camera_id not in tasks]
                for camera_id in rotate_for_admission(candidates, admission_round):
                    if len(tasks) >= max_concurrent_cameras:
                        logger.info(
                            "camera_admission_refused_capacity",
                            extra={
                                "camera_id": str(camera_id),
                                "running_count": len(tasks),
                                "max_concurrent_cameras": max_concurrent_cameras,
                            },
                        )
                        continue
                    assignment = current[camera_id]
                    tasks[camera_id] = asyncio.create_task(
                        _isolated_camera_loop(assignment, run_cycle=build_run_cycle(assignment), stop=stop),
                        name=f"pipeline-camera-{camera_id}",
                    )
                    live_assignments[camera_id] = assignment
                admission_round += 1

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=discovery_interval_seconds)
    finally:
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Stops the loop cleanly on SIGTERM so a container restart finishes the cycle it is
    on - the same helper `notification_worker/app/worker.py` already established."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)


async def amain() -> None:
    settings = get_settings()
    configure_logging("pipeline-runtime", settings.environment, settings.log_level)

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        object_store = create_client(settings)
    except Exception:  # noqa: BLE001 - evidence snapshots are optional; detections/incidents are not
        logger.exception("object_store_unavailable_evidence_disabled")
        object_store = None

    stop = asyncio.Event()
    install_signal_handlers(stop)

    async def _fetch_assignments() -> list[Assignment]:
        async with platform_session(session_factory) as session:
            return await active_cloud_assignments(session)

    async with httpx.AsyncClient(
        base_url=settings.ai_runtime_base_url,
        timeout=httpx.Timeout(settings.ai_runtime_infer_timeout_seconds, connect=5.0),
    ) as http_client:

        def _build_run_cycle(_assignment: Assignment) -> RunCycle:
            return functools.partial(
                run_one_camera_cycle,
                settings=settings,
                session_factory=session_factory,
                http_client=http_client,
                object_store=object_store,
            )

        try:
            await run_discovery_loop(
                fetch_assignments=_fetch_assignments,
                build_run_cycle=_build_run_cycle,
                discovery_interval_seconds=settings.pipeline_runtime_discovery_poll_seconds,
                max_concurrent_cameras=settings.pipeline_runtime_max_concurrent_cameras,
                stop=stop,
            )
        finally:
            await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
