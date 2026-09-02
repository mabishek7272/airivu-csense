"""Detection ingestion endpoints — where an edge device's observations enter the system.

There are two: one detection per request, and a batch of up to `MAX_BATCH`. The batch is
not an optimisation bolted on afterwards - it is what makes an offline device's spool
drainable (FLOW-13 step 6), and it runs every item through the identical pipeline call so
that the two paths cannot behave differently. See `ingest_batch` for its own guarantees.

This is the front door to the pipeline: everything the product does downstream (rules,
incidents, evidence, alerting) begins with a request handled here. Until it existed, all
of that machinery had no production caller.

Three properties this endpoint has to get right, because an edge device is not a browser:

  **It must be safe to retry.** Edge devices buffer and resend after a network drop, since
  the alternative is losing events. Every submission carries a `source_event_id` and the
  pipeline upserts on it, so a replay returns the original outcome rather than opening a
  second incident. A device that retries ten times must not produce ten alerts.

  **It must not trust its input.** The caller is a device in a plant room. Bounding boxes
  are range-checked, object counts are bounded, and the camera is verified to belong to
  the calling tenant before anything is written - row-level security would catch the last
  one, but a clear 404 is better than an opaque constraint violation.

  **Frames are optional but strongly preferred.** A device on a metered link can submit
  detections without imagery and still get incidents and alerts; it just gets no snapshot.
  An alert a guard cannot see is an alert they have to drive to.

**Authentication accepts two shapes of caller, deliberately, not as a stopgap for one**:

  A real enrolled device's own credential (`current_agent` - the identical per-device,
  individually-revocable credential `/heartbeat` and the signed-command endpoints in
  `edge.py` already use). When this is how a request authenticates, `edge_device_id` is
  taken from the credential itself, never from the request body - a device cannot claim
  to be reporting on behalf of a different device it isn't.

  A standard customer-audience access token carrying `detection.ingest`, which the
  `edge_device` role holds and nothing else. This was the *only* path before device
  enrolment/credentials existed (migration 0019's own comment called it "interim"); it
  stays supported for anything that authenticates as a tenant member rather than an
  enrolled device (a test harness, an integration that predates enrolment) - not a gap
  waiting to be closed, a second real path with a real, narrower reason to exist. Its own
  `edge_device_id` remains a self-reported, unverified body field, exactly as it always
  has - there is no device credential behind it to check against.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps_agent import current_agent
from csense_shared.config import get_settings
from csense_shared.errors import ApiError, AuthenticationError, NotFoundError
from csense_shared.pipeline.ingest import MAX_OBJECTS_PER_FRAME, ingest_detection
from csense_shared.pipeline.rules import DetectedObject
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, TokenError, decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/ingest", tags=["ingest"])

# A generous ceiling on an inline frame. Anything larger is a video, not a snapshot, and
# uploading it inline would tie up a request worker for the duration.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# How far into the future a capture timestamp may sit before it is rejected. Edge clocks
# drift and are sometimes wrong by hours; a small tolerance absorbs ordinary skew, while a
# timestamp days ahead would park an incident at the top of every list indefinitely.
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)

# Detections accepted in one batch. A device draining a day-long spool wants this as large
# as possible; a shared API wants it small enough that one caller cannot pin a request
# worker or a connection for long. 100 is the compromise - a thousand-event backlog is ten
# round trips, not a thousand, and the worst case a single request can cost stays bounded.
MAX_BATCH = 100

# Total *compressed* inline frame bytes one batch may carry. `MAX_FRAME_BYTES` bounds a
# single frame, but 100 of them at that ceiling is 800 MB of request body and, far worse,
# `cv2.imdecode` expands each one into raw BGR - a 10-30x multiplier that would materialise
# gigabytes of pixel buffers in a single worker. 32 MiB is roughly four full-size frames or
# sixty ordinary snapshots per batch. Past it, remaining frames are dropped and their
# detections still ingest: the same trade `_decode_frame` already makes for an undecodable
# frame, and the only one that keeps a spool drain from stalling on its own imagery.
MAX_BATCH_FRAME_BYTES = 32 * 1024 * 1024

# How much of pydantic's complaint about a malformed batch item is echoed back. Pydantic's
# own `errors()` carries `input` - which for a missing field is the *entire* submitted item,
# multi-megabyte inline frame included - plus a docs URL and, in `ctx`, live exception
# objects. Returning that raw would reflect the payload back to the caller and into the
# logs, so only `loc` and `msg` are used, at most `MAX_VALIDATION_ERRORS` of them, truncated
# here. A device operator needs the field name and what was wrong with it; anything more is
# for the server's own logs.
MAX_VALIDATION_ERRORS = 3
MAX_VALIDATION_DETAIL_CHARS = 400


@dataclasses.dataclass(frozen=True)
class IngestIdentity:
    tenant_id: uuid.UUID
    # Only ever set when `source == "device"` - a real, credential-verified device
    # identity, never the request body's own (self-reported, unverified) field.
    edge_device_id: uuid.UUID | None
    source: str  # "device" | "user" - see this module's own docstring for what each is


async def current_ingest_identity(request: Request) -> IngestIdentity:
    """Tries the real per-device credential first (`current_agent` - identical to what
    `/heartbeat` and the signed-command endpoints already authenticate with), and falls
    back to a customer-audience access token carrying `detection.ingest` only if that
    fails. Any failure from `current_agent` is treated as "not a device credential", not
    as a rejection in its own right - a token that fails both checks gets one uniform
    401, the same "every failure looks the same" discipline `deps_agent.py` already
    documents for its own lookup.
    """
    try:
        agent = await current_agent(request)
        return IngestIdentity(
            tenant_id=uuid.UUID(agent.tenant_id), edge_device_id=uuid.UUID(agent.device_id), source="device",
        )
    except ApiError:
        pass  # not a device credential - fall through to the legacy scoped-token path

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        raise AuthenticationError("Missing bearer credential.")

    settings = get_settings()
    try:
        claims = decode_access_token(presented.strip(), settings=settings, expected_audience=AUDIENCE_CUSTOMER)
    except TokenError as exc:
        raise AuthenticationError("That credential is not valid.") from exc
    if claims.tenant_id is None or "detection.ingest" not in claims.permissions:
        raise AuthenticationError("That credential is not valid.")

    return IngestIdentity(tenant_id=claims.tenant_id, edge_device_id=None, source="user")


@asynccontextmanager
async def tenant_scoped_session(factory, tenant_id: uuid.UUID):
    """One session in one transaction, scoped to `tenant_id`.

    Factored out of the dependency below because the batch endpoint needs the same scoping
    but a *fresh* transaction per item rather than one spanning the request - see
    `ingest_batch` for why. `set_config(..., true)` keeps the setting local to the
    transaction so it cannot leak to the next checkout of a pooled connection.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)},
        )
        yield session


async def ingest_db_session(
    request: Request, identity: IngestIdentity = Depends(current_ingest_identity)
) -> AsyncSession:
    """Scoped to whichever tenant the identity above resolved to - never a
    client-supplied value, from either auth path."""
    async with tenant_scoped_session(request.app.state.session_factory, identity.tenant_id) as session:
        yield session


class ObjectIn(BaseModel):
    """One detected object, in normalised frame coordinates."""

    class_name: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    # x1, y1, x2, y2 normalised 0..1, so the box means the same thing at any resolution.
    bbox: list[float] = Field(min_length=4, max_length=4)

    @field_validator("bbox")
    @classmethod
    def _normalised_and_ordered(cls, value: list[float]) -> list[float]:
        if any(v < 0.0 or v > 1.0 for v in value):
            raise ValueError("bbox coordinates must be normalised to 0..1")
        x1, y1, x2, y2 = value
        if x2 <= x1 or y2 <= y1:
            # A zero or inverted box has no area, so ROI overlap is always zero and the
            # detection can never match a zone rule. Rejecting it here names the problem
            # at the device instead of silently dropping events.
            raise ValueError("bbox must have positive area with x1<x2 and y1<y2")
        return value


class DetectionIn(BaseModel):
    camera_id: uuid.UUID
    # The device's own idempotency key. Resending the same one is explicitly safe.
    source_event_id: str = Field(min_length=1, max_length=200)
    captured_at: dt.datetime
    objects: list[ObjectIn] = Field(max_length=MAX_OBJECTS_PER_FRAME)
    # Base64 JPEG/PNG of the frame. Optional; without it there is no snapshot.
    frame_base64: str | None = None
    model_version_id: uuid.UUID | None = None
    edge_device_id: uuid.UUID | None = None


class IngestOut(BaseModel):
    detection_id: str
    duplicate: bool
    rules_evaluated: int
    incident_id: uuid.UUID | None = None
    incident_number: int | None = None
    incident_created: bool = False
    notifications_scheduled: int = 0
    evidence_captured: bool = False
    # Why nothing fired, when nothing did. Without this, "the camera sees people and I get
    # no alerts" is only debuggable by reading server logs.
    rejected_reasons: list[str] = []


class DetectionBatchIn(BaseModel):
    """The envelope only. The *items* are deliberately untyped here.

    Typing this `list[DetectionIn]` is the obvious thing to write and it quietly breaks the
    one guarantee the batch endpoint exists to make: pydantic validates every element while
    parsing the request, so a single malformed item returns one 422 for the whole batch,
    with no per-item detail, before `ingest_batch` runs at all. For a device draining an
    offline spool that is fatal - one bad row poisons every batch it is ever drained in, and
    the spool stops draining behind it. `_validate_item` does the identical validation
    per item inside the handler instead, so a malformed item gets its own error entry beside
    its accepted neighbours.

    The envelope itself still validates, because there is nothing partial to preserve when
    the body is not a JSON object with a bounded `detections` list at all. At least one: a
    device with nothing to send has no reason to call, and an empty body is far more likely
    a bug in its drain loop than a deliberate no-op worth accepting.
    """

    detections: list[Any] = Field(min_length=1, max_length=MAX_BATCH)


class BatchItemOut(BaseModel):
    """One detection's outcome, echoing the `source_event_id` it belongs to.

    The device matches on `source_event_id`, not on list position, so that a truncated or
    reordered response can never make it delete the wrong spool row - the one mistake in
    this whole design that silently loses events.
    """

    # None only when the item was too malformed for one to be recovered - see
    # `_recover_source_event_id`. Null rather than a guess: an id the device cannot match
    # authorises deleting nothing, while a wrong one authorises deleting the wrong row.
    source_event_id: str | None
    accepted: bool
    # Exactly what the single endpoint returns, and only when the item was accepted.
    result: IngestOut | None = None
    # The code the single endpoint's own error response would have carried, so a device
    # can name in its logs what it is dealing with.
    error_code: str | None = None
    error_message: str | None = None
    # Whether re-sending these identical bytes could ever succeed - the same field, with
    # the same meaning, as `ProblemResponse.retryable`, and False for anything that is not
    # an error (there is nothing to retry about an acceptance).
    #
    # This is on the response because the alternative is every edge agent keeping its own
    # copy of the server's list of permanent codes, and an edge fleet updates in months
    # while the API updates in days. A permanent code added here would read as "retryable"
    # on every already-deployed agent and hold the row at the head of its spool until a
    # 24-hour deadline expired. The side that knows the answer states it.
    retryable: bool = False


class BatchIngestOut(BaseModel):
    accepted: int
    failed: int
    # In request order, one entry per submitted detection, always.
    results: list[BatchItemOut]


def _decode_frame(frame_base64: str | None):
    """Decodes an inline frame, or returns None.

    A frame that will not decode is logged and dropped rather than failing the request:
    the detection and any resulting alert are far more valuable than the snapshot, and a
    device with a broken encoder should not stop reporting intrusions.
    """
    if not frame_base64:
        return None

    try:
        raw = base64.b64decode(frame_base64, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("frame_base64_invalid")
        return None

    if len(raw) > MAX_FRAME_BYTES:
        logger.warning("frame_too_large", extra={"bytes": len(raw)})
        return None

    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001 - a bad frame must not cost the detection
        logger.exception("frame_decode_failed")
        return None

    if image is None:
        logger.warning("frame_not_an_image")
    return image


def _correlation_uuid(request: Request) -> uuid.UUID | None:
    """The request's correlation id as a UUID.

    The middleware stores it as a string and the client may supply its own header, so an
    unparseable value is dropped rather than raising - losing the ability to trace one
    request is not worth losing the detection.
    """
    raw = getattr(request.state, "correlation_id", None)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def _camera_site(
    session: AsyncSession, *, tenant_id: uuid.UUID, camera_id: uuid.UUID
) -> tuple[uuid.UUID, str | None]:
    """The camera's site and timezone, confirming it belongs to this tenant.

    The timezone matters: rule schedules are written in local time, and evaluating an
    overnight rule against UTC shifts it by the site's offset.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT c.site_id, s.timezone
                FROM cameras c
                JOIN sites s ON s.id = c.site_id
                WHERE c.id = :camera_id AND c.tenant_id = :tenant_id
                """
            ),
            {"camera_id": camera_id, "tenant_id": tenant_id},
        )
    ).first()

    if row is None:
        raise NotFoundError("No such camera in this tenant.")
    return row[0], row[1]


async def _ingest_one(
    db: AsyncSession,
    request: Request,
    *,
    identity: IngestIdentity,
    body: DetectionIn,
    frame: object,
) -> IngestOut:
    """One detection, from validated body to `IngestOut`.

    Both endpoints go through here, so dedup, rule evaluation, incident correlation,
    evidence and notifications cannot drift apart between the single and batch paths -
    a batch that alerted differently from a single submission would make an offline
    device's replayed events behave unlike its live ones, which is the one thing the
    whole spool design cannot tolerate.

    `frame` is already decoded: the callers differ in how they decide whether to decode
    at all (the batch has a budget across the whole request), not in what they do with
    the result.
    """
    captured_at = body.captured_at
    if captured_at.tzinfo is None:
        # A naive timestamp is ambiguous. Assuming UTC is the only defensible reading, and
        # is what an edge device that omits an offset almost always means.
        captured_at = captured_at.replace(tzinfo=dt.UTC)

    if captured_at > dt.datetime.now(dt.UTC) + MAX_CLOCK_SKEW:
        raise ApiError(
            status_code=422,
            code="capture_time_in_future",
            message=(
                "captured_at is more than five minutes in the future. Check the device "
                "clock - a wrong timestamp puts the incident in the wrong place in every "
                "list and breaks time-window rules."
            ),
            # The one rejection here that time itself cures: nothing about the payload is
            # wrong, the device's clock is simply ahead, and the identical bytes are
            # accepted once NTP corrects it or the wall clock catches up. A device that
            # discarded these would throw away real evidence over a flat RTC battery.
            retryable=True,
        )

    site_id, timezone_name = await _camera_site(
        db, tenant_id=identity.tenant_id, camera_id=body.camera_id
    )

    result = await ingest_detection(
        db,
        request.app.state.object_store,
        tenant_id=identity.tenant_id,
        site_id=site_id,
        camera_id=body.camera_id,
        source_event_id=body.source_event_id,
        captured_at=captured_at,
        objects=[
            DetectedObject(o.class_name, o.confidence, tuple(o.bbox)) for o in body.objects
        ],
        frame=frame,
        model_version_id=body.model_version_id,
        # A real device credential's own identity always wins over anything the request
        # body claims - the legacy scoped-token path has no device identity of its own,
        # so it still trusts the body field, exactly as it always has.
        edge_device_id=identity.edge_device_id if identity.source == "device" else body.edge_device_id,
        correlation_id=_correlation_uuid(request),
        site_timezone=timezone_name,
    )

    return IngestOut(
        detection_id=result.detection_id,
        duplicate=result.duplicate,
        rules_evaluated=result.rules_evaluated,
        incident_id=result.incident_id,
        incident_number=result.incident_number,
        incident_created=result.incident_created,
        notifications_scheduled=result.notifications_scheduled,
        evidence_captured=result.evidence_captured,
        rejected_reasons=result.rejected_reasons,
    )


def _recover_source_event_id(raw: object) -> str | None:
    """The `source_event_id` of an item that failed validation, if one survives.

    This is what the device deletes the right spool row on, so it is worth recovering even
    from an item that is otherwise unusable - without it the row is kept and re-sent until
    its 24-hour deadline, holding a slot in every batch behind it.

    Only a non-empty string counts. A number or an object *could* be coerced - the agent
    keys its own rows with `str(...)`, so `str(42)` would in practice match - but that is a
    claim about how one particular device stringifies its own key, and the single mistake
    this design must never make is authorising the deletion of the wrong row. An id that
    cannot be recovered is reported as null instead; the row is then kept, which is the
    safe direction, and cleared eventually by the device's own deadline.

    Length is not checked against `DetectionIn`'s 200-character limit on purpose: an
    over-long id is exactly one of the reasons the item was rejected, and echoing it back
    is what lets the device drop the row now rather than in a day. It is bounded by the
    request body it arrived in, so there is no amplification.
    """
    if isinstance(raw, Mapping):
        value = raw.get("source_event_id")
        if isinstance(value, str) and value:
            return value
    return None


def _validation_message(exc: ValidationError) -> str:
    """A malformed item's complaint, trimmed to what a device operator can act on.

    Deliberately not `exc.errors()` or `exc.json()`: pydantic's error entries carry `input`
    - for a missing field, the *whole* submitted item, inline frame and all - a docs URL,
    and a `ctx` that can hold live exception objects. That would reflect the payload back to
    the caller, put a multi-megabyte base64 frame in the response and the logs, and expose
    the model's internal shape. `loc` and `msg` name the field and what was wrong with it,
    which is the entire actionable content.
    """
    parts = [
        f"{'.'.join(str(p) for p in error.get('loc', ())) or 'body'}: "
        f"{error.get('msg', 'invalid')}"
        for error in exc.errors()[:MAX_VALIDATION_ERRORS]
    ]
    message = (
        "This detection does not match the ingest schema and was not stored; the rest of "
        f"the batch was processed. {'; '.join(parts)}"
    )
    if len(message) > MAX_VALIDATION_DETAIL_CHARS:
        return message[: MAX_VALIDATION_DETAIL_CHARS - 1] + "…"
    return message


def _validate_item(raw: object) -> tuple[DetectionIn | None, BatchItemOut | None]:
    """One raw batch item, as either a validated detection or its own rejection.

    Exactly one of the two is ever set. This is the per-item half of what pydantic would
    otherwise do while parsing the request - see `DetectionBatchIn` for why doing it here
    instead is the whole point of the endpoint.
    """
    try:
        return DetectionIn.model_validate(raw), None
    except ValidationError as exc:
        source_event_id = _recover_source_event_id(raw)
        logger.warning(
            "batch_item_invalid",
            extra={"source_event_id": source_event_id, "errors": exc.error_count()},
        )
        return None, BatchItemOut(
            source_event_id=source_event_id,
            accepted=False,
            error_code="validation_error",
            error_message=_validation_message(exc),
            # The bytes themselves are wrong, and re-sending the identical bytes will be
            # just as wrong next time. The device should drop the row and count the drop.
            retryable=False,
        )


def _apply_frame_budget(
    detections: list[DetectionIn | None], *, budget: int = MAX_BATCH_FRAME_BYTES
) -> list[str | None]:
    """Each detection's inline frame, or None once the batch's frame budget is spent.

    Sized from the base64 length rather than by decoding, because the whole point is to
    avoid materialising the bytes at all - measuring by decoding would already have cost
    what the budget exists to prevent.

    Once the budget is exhausted the *rest* of the batch loses its frames, not just the
    oversized item. That is deliberate and it does penalise later items unfairly; the
    alternative - skipping only the items that do not fit and continuing to accept smaller
    ones - would make which frames survive depend on their sizes rather than their order,
    which is far harder for a device operator to reason about than "the tail of an
    oversized batch has no snapshots, send fewer per batch".
    """
    remaining = budget
    kept: list[str | None] = []
    for item in detections:
        # A None is an item that failed validation: it keeps its slot so the result list
        # stays positional, but it has no frame and spends no budget.
        encoded = item.frame_base64 if item is not None else None
        if not encoded:
            kept.append(None)
            continue
        # Ceiling of the decoded length; padding makes this at most two bytes generous,
        # which does not matter against a multi-megabyte budget.
        decoded_size = (len(encoded) * 3) // 4
        if decoded_size > remaining:
            logger.warning("batch_frame_budget_exhausted", extra={"budget_bytes": budget})
            kept.append(None)
            remaining = 0
            continue
        remaining -= decoded_size
        kept.append(encoded)
    return kept


@router.post("/detections", response_model=IngestOut, status_code=202)
async def ingest(
    body: DetectionIn,
    request: Request,
    identity: IngestIdentity = Depends(current_ingest_identity),
    db: AsyncSession = Depends(ingest_db_session),
) -> IngestOut:
    """Accepts one frame's detections and runs them through the pipeline.

    202 rather than 201: the detection is recorded and any incident opened synchronously,
    but the alerts it schedules are delivered by the notification worker afterwards. The
    device is being told "accepted", not "everyone has been telephoned".

    No `require_permission` call here - `current_ingest_identity` already enforced
    authorization as part of resolving who's calling (either a real device credential,
    or a token that already carried `detection.ingest`), the same way `current_agent`'s
    own callers never call `require_permission` afterwards either.
    """
    return await _ingest_one(
        db, request, identity=identity, body=body, frame=_decode_frame(body.frame_base64)
    )


@router.post("/detections/batch", response_model=BatchIngestOut, status_code=202)
async def ingest_batch(
    body: DetectionBatchIn,
    request: Request,
    identity: IngestIdentity = Depends(current_ingest_identity),
) -> BatchIngestOut:
    """Accepts up to `MAX_BATCH` detections in one request (FLOW-13 step 6).

    This exists for a device draining an offline spool. Doing that one round trip at a
    time means a day's backlog takes a day's worth of round trips to clear, during which
    the device is still accumulating new events - the backlog can lose the race. Batching
    turns a thousand-event drain into ten requests.

    **One bad item never fails the batch.** Every detection gets its own result entry,
    in request order, carrying the `source_event_id` it belongs to. A rejected item -
    unknown camera, a clock days out, a malformed body, an unanticipated failure - returns
    its own error and the rest still process. A device draining a spool must not be
    permanently blocked by one poisoned row: it would retry the batch forever, the spool
    would fill, and the events behind the bad one would be lost to eviction. That is the
    single property this endpoint exists to guarantee.

    **A malformed item is not an exception to that**, which is why the items arrive here
    untyped and are validated one at a time by `_validate_item`. Typing them in
    `DetectionBatchIn` would hand the job to pydantic, which does it while parsing the
    request and answers one 422 for the whole batch - the exact failure above, reached by
    the exact route this endpoint was written to close.

    **Every entry says whether re-sending it could ever work** (`retryable`), so the list
    of permanent failures lives on the side that defines it rather than being copied into
    every deployed edge agent - see `BatchItemOut.retryable`.

    **Transaction boundary: one transaction per item, not one per request.** The single
    endpoint takes its session from `ingest_db_session`, which holds one transaction open
    for the whole request; `ingest_detection` documents why that is right for one
    detection - the detection, its incident and its scheduled notifications land together
    or not at all, because a detection recorded without its alerts looks handled when
    nobody was told. A batch cannot extend that to the whole request in either direction:
    rolling back on item 40 would discard 39 successful, already-acknowledged ingests, and
    a database error inside one item poisons the transaction so that every subsequent
    statement fails anyway. So this handler deliberately does not depend on
    `ingest_db_session` at all - it opens a fresh tenant-scoped transaction per item, and
    each item keeps exactly the all-or-nothing guarantee the single endpoint has. Items
    are processed sequentially for the same reason: concurrency here would let one
    batch open `MAX_BATCH` connections, and would reorder frames of the same event against
    incident correlation, which is order-sensitive.

    **Replaying a batch is safe**, and is expected - a device that loses the response after
    the server committed will resend. Each item carries its own `source_event_id` and goes
    through the identical `ingest_detection` path, which upserts on
    `(tenant_id, source_event_id)`, so a resent batch returns `duplicate: true` per item
    and opens no second incident.

    Inline frames are allowed, but the batch's total decoded frame bytes are capped at
    `MAX_BATCH_FRAME_BYTES` (32 MiB) - see that constant for the reasoning. Frames past
    the budget are dropped; their detections are still ingested, because an alert without
    a snapshot beats no alert.
    """
    factory = request.app.state.session_factory
    # Every item validated up front, so the frame budget is spent in request order over the
    # whole batch rather than shifting depending on where the malformed items happen to sit.
    prepared = [_validate_item(raw) for raw in body.detections]
    # Passed explicitly rather than left to the default so the budget is read at call
    # time - a deployment (or a test) that overrides the constant is actually honoured.
    frames = _apply_frame_budget([item for item, _ in prepared], budget=MAX_BATCH_FRAME_BYTES)

    results: list[BatchItemOut] = []
    for (item, rejection), frame_base64 in zip(prepared, frames, strict=True):
        if rejection is not None:
            # Never reached the pipeline, so there is nothing to roll back and nothing to
            # log beyond what `_validate_item` already logged.
            results.append(rejection)
            continue
        # Decoded outside the session: `cv2.imdecode` on a multi-megabyte frame is not
        # fast, and holding a pooled connection across it would starve other callers.
        frame = _decode_frame(frame_base64)
        try:
            async with tenant_scoped_session(factory, identity.tenant_id) as session:
                outcome = await _ingest_one(
                    session, request, identity=identity, body=item, frame=frame
                )
        except ApiError as exc:
            # A deliberate rejection this endpoint already knows how to describe - the
            # same code and message the single endpoint would have returned for it.
            logger.warning(
                "batch_item_rejected",
                extra={"source_event_id": item.source_event_id, "code": exc.code},
            )
            results.append(
                BatchItemOut(
                    source_event_id=item.source_event_id,
                    accepted=False,
                    error_code=exc.code,
                    error_message=exc.message,
                    # Read off the exception rather than from a table of codes here, so a
                    # rejection classifies itself once, where it is raised, and both
                    # endpoints answer identically. The two reachable today are
                    # `not_found` (False - resending cannot make the camera exist) and
                    # `capture_time_in_future` (True - see where it is raised).
                    #
                    # The trap for whoever adds the third: `ApiError` defaults `retryable`
                    # to False, and a device reads False as "delete this row". A new
                    # rejection that is actually transient must say so explicitly or it
                    # will quietly cost real evidence. It will at least be *visible* loss -
                    # the device counts the discard and the next heartbeat escalates it to
                    # `degraded` - which is why this errs on the exception's own word
                    # rather than second-guessing it with a list of codes here.
                    retryable=exc.retryable,
                )
            )
        except Exception:  # noqa: BLE001 - one poisoned row must not block a drain
            logger.exception(
                "batch_item_failed", extra={"source_event_id": item.source_event_id}
            )
            results.append(
                BatchItemOut(
                    source_event_id=item.source_event_id,
                    accepted=False,
                    error_code="ingest_failed",
                    error_message=(
                        "This detection could not be ingested. The rest of the batch was "
                        "processed; retrying this one alone is safe."
                    ),
                    # Nothing was said about the payload - a deadlock, a full disk, a bug
                    # here. The bytes may well be fine, so the device keeps the row.
                    retryable=True,
                )
            )
        else:
            results.append(
                BatchItemOut(
                    source_event_id=item.source_event_id, accepted=True, result=outcome
                )
            )

    accepted = sum(1 for r in results if r.accepted)
    return BatchIngestOut(
        accepted=accepted, failed=len(results) - accepted, results=results
    )
