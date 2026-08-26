"""Tenant-facing detection listing (TRD §10.2, §17).

A detection on its own is coordinates and a class name, which tells an operator almost
nothing. This endpoint returns what someone actually needs to judge it: the annotated
snapshot, the boundary, the detection id, the capture time, and where it happened - camera
name, site name, and the site's address and coordinates.

Every query runs inside `tenant_session()`, so row-level security scopes results to the
caller's tenant in the database. Handlers never filter by tenant themselves and never
accept a tenant id from the client.

Evidence images are served as short-lived presigned URLs rather than proxied bytes: the
image never passes through this process, and the URL expires. The URL is only minted after
the tenant check, because object storage performs no authorisation of its own (SCH §2).
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext
from csense_shared.storage.objects import create_presign_client

router = APIRouter(prefix="/api/v1/tenant/detections", tags=["detections"])

MAX_PAGE_SIZE = 100
EVIDENCE_URL_TTL = dt.timedelta(minutes=10)


class BoundingBox(BaseModel):
    """Normalised 0..1 against the frame, so a UI can overlay it at any render size."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1


class DetectedObjectOut(BaseModel):
    class_name: str
    confidence: float
    bbox: BoundingBox
    track_id: str | None = None


class LocationOut(BaseModel):
    site_id: str
    site_name: str
    site_code: str
    zone_name: str | None = None
    address: dict | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str


class CameraOut(BaseModel):
    camera_id: str
    camera_name: str
    camera_code: str
    vendor: str | None = None
    model: str | None = None


class EvidenceOut(BaseModel):
    evidence_id: str
    variant: str
    url: str | None = None
    sha256: str
    captured_at: dt.datetime


class DetectionOut(BaseModel):
    detection_id: str
    event_type: str
    source_event_id: str
    confidence: float
    # Source capture time is the one that answers "when did this happen"; the others say
    # when the platform found out, which differs after an offline spool replay.
    captured_at: dt.datetime
    cloud_received_at: dt.datetime
    objects: list[DetectedObjectOut]
    camera: CameraOut
    location: LocationOut
    evidence: list[EvidenceOut]
    incident_id: str | None = None
    incident_number: int | None = None


class DetectionPage(BaseModel):
    items: list[DetectionOut]
    next_cursor: str | None = None


def _encode_cursor(captured_at: dt.datetime, detection_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"t": captured_at.isoformat(), "id": str(detection_id)}).encode()
    ).decode()


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return dt.datetime.fromisoformat(payload["t"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ApiError(
            status_code=400, code="invalid_cursor", message="Pagination cursor is malformed."
        ) from exc


# One query, joined across camera/site/zone. Doing this per row would be N+1 against a
# list that is browsed constantly.
_BASE_SELECT = """
    SELECT d.id, d.event_type, d.source_event_id, d.confidence, d.capture_time,
           d.cloud_receive_time, d.objects,
           c.id, c.name, c.code, c.vendor, c.model,
           s.id, s.name, s.code, s.address_json, s.latitude, s.longitude, s.timezone,
           z.name AS zone_name,
           i.id AS incident_id, i.incident_number
    FROM detections d
    JOIN cameras c ON c.id = d.camera_id
    JOIN sites s ON s.id = d.site_id
    LEFT JOIN zones z ON z.id = c.zone_id
    LEFT JOIN incident_detection_links l ON l.detection_id = d.id
    LEFT JOIN incidents i ON i.id = l.incident_id
"""


def _row_to_detection(row, evidence: list[EvidenceOut]) -> DetectionOut:
    objects = []
    for item in row[6] or []:
        bbox = item.get("bbox") or [0, 0, 0, 0]
        objects.append(
            DetectedObjectOut(
                class_name=item.get("class") or item.get("class_name") or "unknown",
                confidence=float(item.get("confidence", row[3])),
                bbox=BoundingBox(x1=bbox[0], y1=bbox[1], x2=bbox[2], y2=bbox[3]),
                track_id=item.get("track_id"),
            )
        )

    return DetectionOut(
        detection_id=str(row[0]),
        event_type=row[1],
        source_event_id=row[2],
        confidence=float(row[3]),
        captured_at=row[4],
        cloud_received_at=row[5],
        objects=objects,
        camera=CameraOut(
            camera_id=str(row[7]), camera_name=row[8], camera_code=row[9],
            vendor=row[10], model=row[11],
        ),
        location=LocationOut(
            site_id=str(row[12]), site_name=row[13], site_code=row[14],
            zone_name=row[19],
            address=row[15],
            latitude=float(row[16]) if row[16] is not None else None,
            longitude=float(row[17]) if row[17] is not None else None,
            timezone=row[18],
        ),
        evidence=evidence,
        incident_id=str(row[20]) if row[20] else None,
        incident_number=row[21],
    )


async def _evidence_for(
    db: AsyncSession,
    request: Request,
    detection_ids: list[uuid.UUID],
    *,
    include_original: bool,
) -> dict[uuid.UUID, list[EvidenceOut]]:
    """Fetches evidence for a page of detections in one query, with presigned URLs.

    `original` is excluded unless the caller holds `evidence.download`: the unmasked frame
    is the sensitive artifact, and a listing should never hand it out by default.
    """
    if not detection_ids:
        return {}

    variants = ["annotated", "masked"] + (["original"] if include_original else [])
    rows = (
        await db.execute(
            text(
                """
                SELECT e.detection_id, e.id, e.privacy_variant::text, e.sha256, e.capture_time,
                       so.bucket, so.object_key
                FROM evidence e
                JOIN stored_objects so ON so.id = e.object_id
                WHERE e.detection_id = ANY(:ids) AND e.privacy_variant::text = ANY(:variants)
                ORDER BY e.capture_time
                """
            ),
            {"ids": detection_ids, "variants": variants},
        )
    ).all()

    minio = create_presign_client(request.app.state.settings)
    grouped: dict[uuid.UUID, list[EvidenceOut]] = {}
    for detection_id, evidence_id, variant, sha256, capture_time, bucket, object_key in rows:
        try:
            url = minio.presigned_get_object(bucket, object_key, expires=EVIDENCE_URL_TTL)
        except Exception:
            # A missing object should not blank the whole listing; the row still carries
            # its digest and identity so the gap is visible rather than silent.
            url = None
        grouped.setdefault(detection_id, []).append(
            EvidenceOut(
                evidence_id=str(evidence_id), variant=variant, url=url,
                sha256=sha256, captured_at=capture_time,
            )
        )
    return grouped


@router.get("", response_model=DetectionPage)
async def list_detections(
    request: Request,
    camera_id: uuid.UUID | None = Query(default=None),
    site_id: uuid.UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    since: dt.datetime | None = Query(default=None, description="Filter on source capture time"),
    until: dt.datetime | None = Query(default=None),
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    with_evidence_only: bool = Query(default=False, description="Only detections that have a snapshot"),
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DetectionPage:
    require_permission(context, "incident.read")

    filters, params = [], {"limit": limit + 1}
    if camera_id:
        filters.append("d.camera_id = :camera_id")
        params["camera_id"] = camera_id
    if site_id:
        filters.append("d.site_id = :site_id")
        params["site_id"] = site_id
    if event_type:
        filters.append("d.event_type = :event_type")
        params["event_type"] = event_type
    if since:
        filters.append("d.capture_time >= :since")
        params["since"] = since
    if until:
        filters.append("d.capture_time <= :until")
        params["until"] = until
    if min_confidence is not None:
        filters.append("d.confidence >= :min_confidence")
        params["min_confidence"] = min_confidence
    if with_evidence_only:
        filters.append("EXISTS (SELECT 1 FROM evidence e WHERE e.detection_id = d.id)")
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        filters.append("(d.capture_time, d.id) < (:cursor_time, :cursor_id)")
        params["cursor_time"] = cursor_time
        params["cursor_id"] = cursor_id

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = (
        await db.execute(
            text(f"{_BASE_SELECT} {where} ORDER BY d.capture_time DESC, d.id DESC LIMIT :limit"),
            params,
        )
    ).all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    evidence = await _evidence_for(
        db, request, [r[0] for r in rows],
        include_original=context.has_permission("evidence.download"),
    )
    items = [_row_to_detection(row, evidence.get(row[0], [])) for row in rows]
    next_cursor = _encode_cursor(rows[-1][4], rows[-1][0]) if has_more and rows else None
    return DetectionPage(items=items, next_cursor=next_cursor)


@router.get("/{detection_id}", response_model=DetectionOut)
async def get_detection(
    detection_id: uuid.UUID,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DetectionOut:
    require_permission(context, "incident.read")

    row = (
        await db.execute(text(f"{_BASE_SELECT} WHERE d.id = :id"), {"id": detection_id})
    ).first()
    # RLS scoped this already, so "not found" covers both a missing detection and another
    # tenant's - indistinguishable on purpose, so this cannot probe for existence.
    if row is None:
        raise NotFoundError("Detection not found.")

    evidence = await _evidence_for(
        db, request, [detection_id],
        include_original=context.has_permission("evidence.download"),
    )
    return _row_to_detection(row, evidence.get(detection_id, []))
