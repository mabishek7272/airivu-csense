"""Detection ingestion endpoint — where an edge device's observations enter the system.

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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
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


async def ingest_db_session(
    request: Request, identity: IngestIdentity = Depends(current_ingest_identity)
) -> AsyncSession:
    """Scoped to whichever tenant the identity above resolved to - never a
    client-supplied value, from either auth path."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(identity.tenant_id)},
        )
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
        frame=_decode_frame(body.frame_base64),
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
