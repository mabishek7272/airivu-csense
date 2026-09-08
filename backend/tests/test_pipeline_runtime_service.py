"""Tests for `backend/pipeline_runtime/app/main.py` (Task 3 of
docs/superpowers/plans/2026-09-08-pipeline-execution-runtime.md).

Three kinds of test here, matching this task's own spec and its two carried-forward Task 2
code-review warnings:

  - **Real `asyncio.Task` spawn/cancel/isolation mechanics** for `run_discovery_loop` /
    `_camera_loop` / `_isolated_camera_loop` - the actual subject of this task's own spec.
    `fetch_assignments`/`build_run_cycle` are injected fakes (no real database, camera, or
    ai-runtime call), but the tasks themselves are real `asyncio.Task`s scheduled on the
    real event loop, cancelled and awaited for real - nothing about task lifecycle itself
    is mocked away.
  - **Real, observed concurrency** proving `grab_frame_for_assignment` actually dispatches
    the blocking frame grab to an executor (Task 2 review warning #2) rather than calling
    it inline, and that `sync_frame_closure` is genuinely a plain synchronous callable,
    never a coroutine function (Task 2 review warning #1) - both by running real asyncio
    machinery and observing what it actually does, not by inspecting source.
  - **A real, throwaway MediaMTX RTSP source and the real, built `pipeline-runtime` Docker
    image**, proving a real frame decodes *inside the container* - the same pattern
    `backend/tests/test_frame_grab.py` already established for Task 1, extended one layer
    out (a second container reaching the first over a real Docker network, rather than the
    test process itself).

Each backend service names its own package `app`, so a plain `from app.main import ...`
would resolve to whichever service's `app` package Python happened to import first -
`backend/tests/test_notification_worker.py`'s own `_load_worker` already solved this by
loading the module by file path under a private name; this file does the same for
`pipeline_runtime/app/main.py`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import socket
import subprocess
import threading
import time
import types
import urllib.error
import urllib.request
import uuid

import numpy as np
import pytest

# No module-wide asyncio mark needed or wanted here: pytest.ini's asyncio_mode = auto
# (the same convention test_pipeline_runtime.py already relies on) picks up every
# `async def test_*` automatically. A blanket `pytestmark = pytest.mark.asyncio` was
# tried here first and produced a real (if harmless) warning on this file's handful of
# deliberately-sync tests (the pure interval-math and sync-closure-shape checks below),
# which don't need or want the asyncio machinery at all.


def _load_pipeline_runtime_main():
    path = (
        pathlib.Path(__file__).resolve().parents[1] / "pipeline_runtime" / "app" / "main.py"
    )
    spec = importlib.util.spec_from_file_location("csense_pipeline_runtime_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_main = _load_pipeline_runtime_main()

from csense_shared.pipeline.runtime import Assignment  # noqa: E402


def _assignment(**overrides) -> Assignment:
    now = dt.datetime.now(dt.UTC)
    defaults = dict(
        assignment_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        camera_id=uuid.uuid4(),
        hostname="192.0.2.10",
        rtsp_port=554,
        main_stream_path="/main",
        username=None,
        connection_mode="direct",
        model_name="yolov8n-general",
        confidence=0.5,
        sample_fps=40.0,  # a fast, test-only cadence - real deployments use ~0.5fps
        effective_from=now - dt.timedelta(days=1),
        effective_to=None,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


# --- interval_seconds / sync_frame_closure: pure, no I/O at all -------------------------


def test_interval_seconds_is_the_reciprocal_of_sample_fps():
    assert _main.interval_seconds(0.5) == pytest.approx(2.0)
    assert _main.interval_seconds(2.0) == pytest.approx(0.5)


def test_interval_seconds_never_produces_a_zero_or_negative_sleep():
    """A malformed `sample_fps` (0 or negative) must not turn into a busy loop hammering
    the database and ai-runtime with no delay at all."""
    assert _main.interval_seconds(0.0) > 0
    assert _main.interval_seconds(-1.0) > 0


def test_rotate_for_admission_returns_empty_for_no_candidates():
    assert _main.rotate_for_admission([], admission_round=0) == []
    assert _main.rotate_for_admission([], admission_round=7) == []


def test_rotate_for_admission_rotates_the_starting_offset_by_round():
    """Round 0 is the identity order; each later round moves the starting point one
    further along, wrapping around - so which waiting candidate is "first in line" for
    the next open slot changes cycle over cycle instead of always being the same one
    (see `rotate_for_admission`'s own docstring for why this matters)."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = [a, b, c]

    assert _main.rotate_for_admission(candidates, admission_round=0) == [a, b, c]
    assert _main.rotate_for_admission(candidates, admission_round=1) == [b, c, a]
    assert _main.rotate_for_admission(candidates, admission_round=2) == [c, a, b]
    assert _main.rotate_for_admission(candidates, admission_round=3) == [a, b, c]  # wraps


def test_sync_frame_closure_is_a_plain_callable_never_a_coroutine_function():
    """Task 2 code-review warning #1: `run_one_cycle` calls `grab_frame_fn` unawaited, so
    this closure must never be `async def` - a coroutine function here would silently
    defeat the `if frame is None` unreachable-camera check with a non-None coroutine
    object instead of a real frame or a real absence of one."""
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    fn = _main.sync_frame_closure(frame)

    assert not asyncio.iscoroutinefunction(fn)
    assert fn(_assignment()) is frame


def test_sync_frame_closure_around_none_returns_none_not_a_coroutine():
    fn = _main.sync_frame_closure(None)
    assert not asyncio.iscoroutinefunction(fn)
    assert fn(_assignment()) is None


# --- grab_frame_for_assignment: real executor dispatch, no real camera/DB ---------------


async def test_grab_frame_for_assignment_dispatches_the_blocking_call_to_an_executor():
    """Task 2 code-review warning #2, proven for real: a fake `grab_frame_fn` that blocks
    for 300ms via `time.sleep` (a real OS-level block, not `asyncio.sleep`) must not stall
    a concurrently-running coroutine on the same event loop. `time.sleep` inline on the
    loop thread would starve `_heartbeat` almost completely; dispatched to
    `loop.run_in_executor`, `_heartbeat` keeps ticking on schedule throughout.
    """
    assignment = _assignment()
    calling_thread = threading.current_thread()
    observed_threads: list[threading.Thread] = []

    async def _fake_resolve_url(_assignment, *, settings, session_factory):
        return "rtsp://198.51.100.10:554/main"  # never actually dialled - grab_frame_fn is faked

    def _fake_blocking_grab_frame(url, *, timeout_seconds):
        assert url == "rtsp://198.51.100.10:554/main"
        assert timeout_seconds == 5.0
        observed_threads.append(threading.current_thread())
        time.sleep(0.3)
        return np.ones((2, 2, 3), dtype=np.uint8)

    heartbeats: list[float] = []

    async def _heartbeat() -> None:
        started = time.monotonic()
        while time.monotonic() - started < 0.32:
            heartbeats.append(time.monotonic())
            await asyncio.sleep(0.02)

    heartbeat_task = asyncio.create_task(_heartbeat())
    # grab_frame_for_assignment reads settings.pipeline_runtime_frame_grab_timeout_seconds
    # unconditionally once a URL resolves (to pass timeout_seconds through to grab_frame_fn)
    # - unlike the sibling test below, this path doesn't short-circuit before touching
    # settings, so a bare None here would raise AttributeError rather than exercise the
    # real executor-dispatch behavior this test exists to prove. A minimal stand-in with
    # just that one attribute is enough; nothing else on `settings` is read on this path.
    fake_settings = types.SimpleNamespace(pipeline_runtime_frame_grab_timeout_seconds=5.0)
    frame = await _main.grab_frame_for_assignment(
        assignment,
        settings=fake_settings,
        session_factory=None,
        grab_frame_fn=_fake_blocking_grab_frame,
        resolve_url_fn=_fake_resolve_url,
    )
    await heartbeat_task

    assert frame is not None and frame.shape == (2, 2, 3)
    assert observed_threads and observed_threads[0] is not calling_thread, (
        "grab_frame_fn ran on the event-loop thread itself, not an executor thread - the "
        "blocking call was made inline"
    )
    assert len(heartbeats) >= 8, (
        f"only {len(heartbeats)} heartbeats observed while the blocking call was in "
        "flight - the event loop was stalled, meaning grab_frame_fn was not actually "
        "dispatched to an executor"
    )


async def test_grab_frame_for_assignment_returns_none_when_url_resolution_fails():
    """A camera whose endpoint can't be resolved right now (blocked address, unreadable
    credential - both handled inside the real `rtsp_url_for_assignment`) must read as
    'no frame', never raise - `resolve_url_fn` returning `None` is exactly that outcome."""

    async def _fake_resolve_url(_assignment, *, settings, session_factory):
        return None

    grabbed: list[str] = []

    def _fake_blocking_grab_frame(url, *, timeout_seconds):
        grabbed.append(url)
        return np.ones((2, 2, 3), dtype=np.uint8)

    frame = await _main.grab_frame_for_assignment(
        _assignment(),
        settings=None,
        session_factory=None,
        grab_frame_fn=_fake_blocking_grab_frame,
        resolve_url_fn=_fake_resolve_url,
    )

    assert frame is None
    assert grabbed == [], "grab_frame_fn must never be called when there is no URL to dial"


# --- run_discovery_loop / _camera_loop / _isolated_camera_loop: real asyncio.Task --------
# lifecycle, no real database, camera, or ai-runtime call.


class _DiscoveryHarness:
    """Runs `run_discovery_loop` as a real background `asyncio.Task` against an injected,
    mutable assignment set, and stops it cleanly at the end of a `with` block. Every test
    below mutates `.active` (and/or raises from a fake `fetch_assignments`) to simulate a
    real discovery poll picking up a new/changed/removed assignment.
    """

    def __init__(
        self,
        *,
        discovery_interval_seconds: float = 0.03,
        # Large enough not to interfere with any test that isn't specifically about the
        # cap itself - matching this task's own "existing tests must still pass with a
        # cap large enough not to interfere" requirement.
        max_concurrent_cameras: int = 1_000_000,
    ) -> None:
        self.active: list[Assignment] = []
        self.calls: dict[uuid.UUID, list[dt.datetime]] = {}
        self.poll_count = 0
        self.poll_should_raise = False
        self._run_cycle_factory = None
        self.stop = asyncio.Event()
        self._discovery_interval_seconds = discovery_interval_seconds
        self._max_concurrent_cameras = max_concurrent_cameras
        self._task: asyncio.Task | None = None

    async def _fetch_assignments(self) -> list[Assignment]:
        self.poll_count += 1
        if self.poll_should_raise:
            raise RuntimeError("simulated transient discovery failure")
        return list(self.active)

    def _default_build_run_cycle(self, assignment: Assignment):
        self.calls.setdefault(assignment.camera_id, [])

        async def _run_cycle(a: Assignment, now: dt.datetime) -> str:
            self.calls[a.camera_id].append(now)
            return "clean"

        return _run_cycle

    def build_run_cycle(self, assignment: Assignment):
        if self._run_cycle_factory is not None:
            return self._run_cycle_factory(assignment)
        return self._default_build_run_cycle(assignment)

    async def __aenter__(self) -> _DiscoveryHarness:
        self._task = asyncio.create_task(
            _main.run_discovery_loop(
                fetch_assignments=self._fetch_assignments,
                build_run_cycle=self.build_run_cycle,
                discovery_interval_seconds=self._discovery_interval_seconds,
                max_concurrent_cameras=self._max_concurrent_cameras,
                stop=self.stop,
            )
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.stop.set()
        await asyncio.wait_for(self._task, timeout=5.0)

    async def calls_for(self, camera_id: uuid.UUID, *, at_least: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.calls.get(camera_id, [])) < at_least and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert len(self.calls.get(camera_id, [])) >= at_least, (
            f"camera {camera_id} only received {len(self.calls.get(camera_id, []))} "
            f"run_cycle calls within {timeout}s, expected at least {at_least}"
        )


async def test_run_discovery_loop_spawns_a_separate_real_task_per_active_camera():
    """Not a shared poll loop: two cameras, each getting their own independent run_cycle
    call stream - the plan's own 'one asyncio task per camera' requirement, proven by both
    receiving several real calls concurrently rather than one call each in lockstep."""
    cam_a, cam_b = _assignment(), _assignment()
    async with _DiscoveryHarness() as harness:
        harness.active = [cam_a, cam_b]
        await harness.calls_for(cam_a.camera_id, at_least=3)
        await harness.calls_for(cam_b.camera_id, at_least=3)


async def test_newly_assigned_camera_is_picked_up_within_one_discovery_interval():
    """A tenant assigning a pipeline reasonably expects it to start working soon - seconds,
    not minutes. Starts with no active assignments at all, then adds one and confirms its
    task starts calling run_cycle promptly, well within a handful of the short discovery
    interval used here."""
    cam = _assignment()
    async with _DiscoveryHarness(discovery_interval_seconds=0.03) as harness:
        await asyncio.sleep(0.1)
        assert harness.calls.get(cam.camera_id, []) == [], "nothing should run before assignment"

        added_at = time.monotonic()
        harness.active = [cam]
        await harness.calls_for(cam.camera_id, at_least=1, timeout=1.0)

        assert time.monotonic() - added_at < 0.5


async def test_revoked_assignment_task_is_cancelled_and_makes_no_further_run_cycle_calls():
    """Revoke an assignment mid-run: its task must stop within one discovery cycle and
    must not make any *further* run_cycle calls afterwards - not just stop mattering."""
    cam = _assignment()
    async with _DiscoveryHarness(discovery_interval_seconds=0.03) as harness:
        harness.active = [cam]
        await harness.calls_for(cam.camera_id, at_least=3)

        harness.active = []  # the revoke
        await asyncio.sleep(0.15)  # several discovery intervals
        count_right_after_revoke = len(harness.calls[cam.camera_id])

        await asyncio.sleep(0.3)  # if the task were still alive, it would have run many more cycles by now
        count_later = len(harness.calls[cam.camera_id])

        assert count_later == count_right_after_revoke, (
            "run_cycle was called again after the assignment was revoked - the camera's "
            "task was not actually cancelled"
        )


async def test_a_changed_assignment_restarts_the_cameras_task_with_the_new_config():
    """Not only a revoke: a tenant_overrides edit (or a pipeline moving to deprecated
    underneath a running camera) is a *different* `Assignment` for the same camera_id, and
    must also be picked up within one discovery interval - the same restart mechanism
    that handles a revoke handles this, since `Assignment` equality is a real field
    comparison, not identity."""
    camera_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    site_id = uuid.uuid4()
    original = _assignment(camera_id=camera_id, tenant_id=tenant_id, site_id=site_id, confidence=0.5)
    updated = _assignment(camera_id=camera_id, tenant_id=tenant_id, site_id=site_id, confidence=0.9)

    seen_confidences: list[float] = []

    class _Harness(_DiscoveryHarness):
        def build_run_cycle(self, assignment: Assignment):
            async def _run_cycle(a: Assignment, now: dt.datetime) -> str:
                seen_confidences.append(a.confidence)
                return "clean"

            return _run_cycle

    async with _Harness(discovery_interval_seconds=0.03) as harness:
        harness.active = [original]
        deadline = time.monotonic() + 2.0
        while 0.5 not in seen_confidences and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert 0.5 in seen_confidences

        harness.active = [updated]
        deadline = time.monotonic() + 2.0
        while 0.9 not in seen_confidences and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert 0.9 in seen_confidences, "the changed assignment's task never started"


async def test_one_cameras_exception_never_stops_its_own_or_another_cameras_task():
    """The equivalent of `notification_worker/app/main.py`'s own
    `_webhook_dispatch_never_takes_alerts_down_with_it` isolation, adapted for per-camera
    tasks: one camera's run_cycle raising on every single call must not end that camera's
    own task (it keeps being retried on schedule) nor a completely different camera's."""
    exploding = _assignment()
    healthy = _assignment()

    class _Harness(_DiscoveryHarness):
        def build_run_cycle(self, assignment: Assignment):
            self.calls.setdefault(assignment.camera_id, [])
            if assignment.camera_id == exploding.camera_id:
                async def _run_cycle(a: Assignment, now: dt.datetime) -> str:
                    self.calls[a.camera_id].append(now)
                    raise RuntimeError("boom - this camera's cycle always fails")

                return _run_cycle

            async def _run_cycle(a: Assignment, now: dt.datetime) -> str:
                self.calls[a.camera_id].append(now)
                return "clean"

            return _run_cycle

    async with _Harness() as harness:
        harness.active = [exploding, healthy]
        await harness.calls_for(exploding.camera_id, at_least=3)
        await harness.calls_for(healthy.camera_id, at_least=3)


async def test_a_failed_discovery_poll_does_not_stop_an_already_running_cameras_task():
    """A transient database hiccup on one discovery poll must not tear down any
    already-running camera task, and the discovery loop itself must keep polling
    afterwards rather than dying."""
    cam = _assignment()

    async with _DiscoveryHarness(discovery_interval_seconds=0.02) as harness:
        harness.active = [cam]
        await harness.calls_for(cam.camera_id, at_least=2)

        harness.poll_should_raise = True
        polls_before = harness.poll_count
        await asyncio.sleep(0.15)
        assert harness.poll_count > polls_before, "the discovery loop stopped polling entirely"

        # The already-running camera task must be untouched by the failed poll(s) above.
        harness.poll_should_raise = False
        calls_before_recovery = len(harness.calls[cam.camera_id])
        await harness.calls_for(cam.camera_id, at_least=calls_before_recovery + 2)


# --- Admission control: max_concurrent_cameras -----------------------------------------


async def test_at_capacity_a_new_assignment_is_not_spawned_and_the_running_task_is_untouched():
    """The cap's whole point: once it's saturated, a newly-discovered active assignment
    must not get a task - and the already-running one must not be disturbed by the new
    candidate showing up (no cancel-and-respawn thrashing)."""
    cam_a, cam_b = _assignment(), _assignment()
    build_counts: dict[uuid.UUID, int] = {}

    class _Harness(_DiscoveryHarness):
        def build_run_cycle(self, assignment: Assignment):
            build_counts[assignment.camera_id] = build_counts.get(assignment.camera_id, 0) + 1
            return super().build_run_cycle(assignment)

    async with _Harness(max_concurrent_cameras=1) as harness:
        harness.active = [cam_a]
        await harness.calls_for(cam_a.camera_id, at_least=3)

        harness.active = [cam_a, cam_b]
        await asyncio.sleep(0.15)  # several discovery intervals

        assert harness.calls.get(cam_b.camera_id, []) == [], (
            "cam_b must never run while the cap is saturated by cam_a"
        )
        assert build_counts[cam_a.camera_id] == 1, (
            "cam_a's task must not have been cancelled and respawned just because a new "
            "candidate showed up while at capacity"
        )

        # cam_a keeps running normally, untouched by cam_b's presence.
        count_before = len(harness.calls[cam_a.camera_id])
        await harness.calls_for(cam_a.camera_id, at_least=count_before + 2)


async def test_a_turned_away_assignment_is_admitted_on_the_next_cycle_after_a_slot_frees():
    """Confirms the "falls out naturally from recompute every cycle" claim for real,
    rather than assuming it: revoke the camera occupying the only slot, and the
    previously-refused one must be picked up on the very next discovery cycle."""
    cam_a, cam_b = _assignment(), _assignment()
    async with _DiscoveryHarness(max_concurrent_cameras=1) as harness:
        harness.active = [cam_a, cam_b]
        await harness.calls_for(cam_a.camera_id, at_least=2)
        assert harness.calls.get(cam_b.camera_id, []) == [], "cam_b should be turned away first"

        harness.active = [cam_b]  # revoke cam_a - frees the only slot
        await harness.calls_for(cam_b.camera_id, at_least=2, timeout=1.0)


async def test_capacity_refusal_is_logged_with_the_camera_id_and_running_count(caplog):
    """No existing test in this file asserts on log output, so this follows
    `test_edge_spool_crypto.py`/`test_envelope.py`'s own established `caplog.at_level` +
    substring-on-`caplog.text` pattern rather than inventing a new one.

    Code-quality review found the original version of this admission-refusal log fired
    at INFO per refused candidate, per cycle - unbounded volume at fleet scale. Fixed by
    moving the per-camera detail to DEBUG and adding one INFO-level aggregate summary per
    cycle instead; this test now checks both levels explicitly, matching what actually
    ships rather than only the DEBUG-level detail or only the INFO-level summary."""
    cam_a, cam_b = _assignment(), _assignment()
    with caplog.at_level("DEBUG"):
        async with _DiscoveryHarness(max_concurrent_cameras=1) as harness:
            harness.active = [cam_a]
            await harness.calls_for(cam_a.camera_id, at_least=1)

            harness.active = [cam_a, cam_b]
            await asyncio.sleep(0.15)

    assert "camera_admission_refused_capacity" in caplog.text
    refusal_records = [
        r for r in caplog.records if r.getMessage() == "camera_admission_refused_capacity"
    ]
    assert refusal_records, "expected at least one camera_admission_refused_capacity DEBUG record"
    record = refusal_records[0]
    assert record.camera_id == str(cam_b.camera_id)
    assert record.running_count == 1
    assert record.max_concurrent_cameras == 1

    summary_records = [
        r for r in caplog.records if r.getMessage() == "camera_admission_refused_capacity_summary"
    ]
    assert summary_records, "expected at least one INFO-level aggregate summary record"
    summary = summary_records[0]
    assert summary.levelname == "INFO"
    assert summary.refused_count >= 1
    assert summary.running_count == 1
    assert summary.max_concurrent_cameras == 1


# --- Real container decode: the real, built pipeline-runtime image against a real, ------
# throwaway MediaMTX RTSP source, reachable over a real Docker network (Task 1's own
# pattern, one layer out: two containers instead of one test process and one container).

MEDIAMTX_IMAGE = "bluenviron/mediamtx:1.20.1-ffmpeg"  # same pinned tag as infra/docker-compose.yml
PIPELINE_RUNTIME_TEST_IMAGE = "csense-pipeline-runtime-test"

pytestmark_container = pytest.mark.skipif(
    shutil.which("docker") is None or shutil.which("ffmpeg") is None,
    reason="docker and a host ffmpeg are both required to build+run the real image and "
    "publish a real RTSP test stream",
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_tcp(host: str, port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"{host}:{port} never accepted a connection: {last_error}")


def _wait_for_path_ready(api_port: int, path_name: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{api_port}/v3/paths/get/{path_name}", timeout=1.0
            ) as resp:
                if json.loads(resp.read()).get("ready"):
                    return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mediamtx path '{path_name}' never became ready")


@pytest.mark.skipif(
    shutil.which("docker") is None or shutil.which("ffmpeg") is None,
    reason="docker and a host ffmpeg are both required to build+run the real image and "
    "publish a real RTSP test stream",
)
def test_the_built_pipeline_runtime_image_decodes_a_real_rtsp_frame_inside_the_container():
    """Builds the real `pipeline_runtime/Dockerfile` image, runs a throwaway MediaMTX
    server with a real ffmpeg-published test pattern (identical fixture shape to
    `test_frame_grab.py`'s own `real_rtsp_stream`), joins both to a private Docker
    network, and runs `grab_frame` *inside a container started from the built image* -
    proving the image's own opencv-python-headless + ffmpeg/RTSP shared libraries actually
    work, not just the host's.
    """
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    network_name = f"csense-pipeline-runtime-test-{uuid.uuid4().hex[:8]}"
    mediamtx_name = f"csense-pipeline-runtime-test-mtx-{uuid.uuid4().hex[:8]}"
    rtsp_port = _free_tcp_port()
    api_port = _free_tcp_port()
    path_name = "testpath"

    subprocess.run(
        ["docker", "build", "-q", "-f", "pipeline_runtime/Dockerfile", "-t", PIPELINE_RUNTIME_TEST_IMAGE, "."],
        cwd=backend_root, check=True, capture_output=True, text=True,
    )

    subprocess.run(["docker", "network", "create", network_name], check=True, capture_output=True, text=True)

    config_dir = pathlib.Path(subprocess.run(
        ["mktemp", "-d"], check=True, capture_output=True, text=True
    ).stdout.strip())
    config_path = config_dir / "mediamtx.yml"
    config_path.write_text(
        "logLevel: error\n"
        "rtspAddress: :8554\n"
        "api: yes\n"
        "apiAddress: :9997\n"
        "authInternalUsers:\n"
        "  - user: any\n"
        "    pass:\n"
        "    ips: []\n"
        "    permissions:\n"
        "      - action: api\n"
        "      - action: publish\n"
        "      - action: read\n"
        "      - action: playback\n"
        "paths:\n"
        "  all_others:\n"
    )

    publisher: subprocess.Popen | None = None
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", mediamtx_name,
                "--network", network_name,
                "-p", f"127.0.0.1:{rtsp_port}:8554",
                "-p", f"127.0.0.1:{api_port}:9997",
                "-v", f"{config_path}:/mediamtx.yml:ro",
                MEDIAMTX_IMAGE,
            ],
            check=True, capture_output=True, text=True,
        )
        _wait_for_tcp("127.0.0.1", rtsp_port, timeout=15.0)

        publisher = subprocess.Popen(
            [
                "ffmpeg", "-y", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                "-t", "120", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15",
                "-f", "rtsp", "-rtsp_transport", "tcp",
                f"rtsp://127.0.0.1:{rtsp_port}/{path_name}",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_for_path_ready(api_port, path_name, timeout=15.0)

        script = (
            "from csense_shared.cameras.frame_grab import grab_frame\n"
            f"frame = grab_frame('rtsp://{mediamtx_name}:8554/{path_name}', timeout_seconds=10.0)\n"
            "if frame is None:\n"
            "    print('FRAME_NONE')\n"
            "else:\n"
            "    print(f'FRAME_OK shape={frame.shape} std={float(frame.std()):.2f}')\n"
        )
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--network", network_name,
                PIPELINE_RUNTIME_TEST_IMAGE, "python", "-c", script,
            ],
            capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, (
            f"pipeline-runtime container failed to run at all: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "FRAME_OK" in result.stdout, (
            f"the container did not decode a real frame: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # testsrc is a colour-bar/gradient pattern; a genuinely decoded frame has real
        # variation, not the near-zero variance of an all-black/undecoded buffer -
        # cross-checking the reported std, not just the presence of the marker string.
        std_str = result.stdout.split("std=")[1].split()[0].strip()
        assert float(std_str) > 10.0
    finally:
        if publisher is not None:
            publisher.terminate()
            try:
                publisher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                publisher.kill()
        subprocess.run(["docker", "rm", "-f", mediamtx_name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", network_name], capture_output=True, check=False)
