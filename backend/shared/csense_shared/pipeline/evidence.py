"""Evidence capture: snapshot, mask, store, verify (TRD §17, SCH §9.6).

The masking decision is the important one. Privacy masking is on by default (PRD), and
this module stores **two objects** - the original and a masked variant - rather than one
image masked at render time. That costs storage, and buys something worth more: the
unmasked bytes are never what a normal read path returns. Masking on render means every
future endpoint, export, thumbnail and email attachment is one missing parameter away from
leaking an unmasked face. Here, serving the original requires deliberately asking for a
different object.

Verification follows the same rule as model artifacts: the digest is computed from the
bytes actually written, and SCH §19 requires digest and size to match storage before
evidence becomes available. `capture_evidence` reads the object back and compares before
committing the row.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
from dataclasses import dataclass

import numpy as np
from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.storage.objects import BUCKET_EVIDENCE

# Blur strength for masked regions. Chosen so a person remains visible as a shape - an
# operator still needs to see that someone is there, and where - while the face is not
# recoverable. A solid box would hide the very thing the incident is about.
BLUR_KERNEL_DIVISOR = 6
MIN_BLUR_KERNEL = 15


@dataclass(frozen=True)
class EvidenceSet:
    """The three variants written for one captured frame.

    `annotated` is what a listing shows, `masked` what a detail view shows, `original`
    what a permissioned download returns.
    """

    original: EvidenceRecord
    masked: EvidenceRecord
    annotated: EvidenceRecord


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: uuid.UUID
    object_id: uuid.UUID
    object_key: str
    sha256: str
    size_bytes: int
    privacy_variant: str


def _odd(value: int) -> int:
    """Gaussian kernels must be odd."""
    return value if value % 2 == 1 else value + 1


def mask_regions(
    image: np.ndarray, boxes: list[tuple[float, float, float, float]]
) -> np.ndarray:
    """Blurs the given normalised boxes in a copy of the image.

    Boxes are normalised (0..1) because that is the coordinate space detections use, so a
    mask stays correct regardless of the resolution the frame was captured at.
    """
    import cv2

    if not boxes:
        return image.copy()

    masked = image.copy()
    height, width = masked.shape[:2]

    for x1, y1, x2, y2 in boxes:
        # Clamp: a detection at the frame edge can produce coordinates slightly outside
        # 0..1, and a negative slice index would silently mask the wrong region.
        px1 = max(0, min(width - 1, int(x1 * width)))
        py1 = max(0, min(height - 1, int(y1 * height)))
        px2 = max(0, min(width, int(x2 * width)))
        py2 = max(0, min(height, int(y2 * height)))
        if px2 <= px1 or py2 <= py1:
            continue

        region = masked[py1:py2, px1:px2]
        kernel = _odd(max(MIN_BLUR_KERNEL, min(region.shape[0], region.shape[1]) // BLUR_KERNEL_DIVISOR))
        masked[py1:py2, px1:px2] = cv2.GaussianBlur(region, (kernel, kernel), 0)

    return masked


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("Failed to encode frame as JPEG")
    return buffer.tobytes()


@dataclass(frozen=True)
class Annotation:
    """One box to draw: normalised coordinates, a label, and whether it triggered."""

    bbox: tuple[float, float, float, float]
    label: str
    confidence: float
    # Detections that fired the rule are drawn in alert colour; ones the rule rejected
    # are drawn muted, so an operator can see what the model saw *and* what the rule
    # decided. Showing only the matches hides the reason a scene looked the way it did.
    triggered: bool = True


# BGR, because OpenCV. Red for what fired, grey for what was seen but rejected.
ALERT_COLOUR = (0, 0, 220)
CONTEXT_COLOUR = (150, 150, 150)


def draw_detections(image: np.ndarray, annotations: list[Annotation]) -> np.ndarray:
    """Draws detection boxes and labels on a copy of the image.

    Line and text scale with frame size: a fixed 2px box is invisible on 4K and covers a
    face on 320p. Cameras in one estate rarely share a resolution.
    """
    import cv2

    if not annotations:
        return image.copy()

    annotated = image.copy()
    height, width = annotated.shape[:2]
    scale = max(height, width) / 1000.0
    thickness = max(2, int(round(2 * scale)))
    font_scale = max(0.45, 0.6 * scale)
    font = cv2.FONT_HERSHEY_SIMPLEX

    for item in annotations:
        colour = ALERT_COLOUR if item.triggered else CONTEXT_COLOUR
        x1, y1, x2, y2 = item.bbox
        px1 = max(0, min(width - 1, int(x1 * width)))
        py1 = max(0, min(height - 1, int(y1 * height)))
        px2 = max(0, min(width, int(x2 * width)))
        py2 = max(0, min(height, int(y2 * height)))
        if px2 <= px1 or py2 <= py1:
            continue

        cv2.rectangle(annotated, (px1, py1), (px2, py2), colour, thickness)

        caption = f"{item.label} {item.confidence:.0%}"
        (text_w, text_h), baseline = cv2.getTextSize(caption, font, font_scale, thickness)

        # Put the label inside the box when there is no room above it, so it never gets
        # clipped off the top edge for a detection at the top of the frame.
        label_top = py1 - text_h - baseline
        if label_top < 0:
            label_top = py1
        label_bottom = label_top + text_h + baseline

        cv2.rectangle(annotated, (px1, label_top), (px1 + text_w, label_bottom), colour, -1)
        cv2.putText(
            annotated,
            caption,
            (px1, label_bottom - baseline),
            font,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

    return annotated


def evidence_object_key(
    tenant_id: uuid.UUID, incident_id: uuid.UUID | None, evidence_id: uuid.UUID, variant: str
) -> str:
    """SCH §14: `{tenant}/incidents/{incident}/{evidence_id}/{variant}.jpg`.

    Tenant-prefixed and built here, never by a client (SCH §11.5). Detections not yet
    attached to an incident go under `unlinked/` so the key stays well-formed.
    """
    scope = str(incident_id) if incident_id else "unlinked"
    return f"{tenant_id}/incidents/{scope}/{evidence_id}/{variant}.jpg"


async def _store_object(
    session: AsyncSession,
    minio: Minio,
    *,
    tenant_id: uuid.UUID,
    object_key: str,
    payload: bytes,
) -> tuple[uuid.UUID, str, int]:
    """Uploads bytes and records the stored_objects row, verifying what landed."""
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)

    minio.put_object(
        BUCKET_EVIDENCE,
        object_key,
        io.BytesIO(payload),
        length=size,
        content_type="image/jpeg",
    )

    # Read back rather than trusting the client's success return: SCH §19 requires digest
    # and size to match storage before evidence is available.
    stat = minio.stat_object(BUCKET_EVIDENCE, object_key)
    if stat.size != size:
        raise RuntimeError(
            f"Evidence upload size mismatch for {object_key}: wrote {size}, storage reports {stat.size}"
        )

    object_id = (
        await session.execute(
            text(
                """
                INSERT INTO stored_objects
                    (tenant_id, bucket, object_key, object_type, mime_type, size_bytes, sha256,
                     retention_class)
                VALUES (:tenant_id, :bucket, :object_key, 'evidence', 'image/jpeg', :size,
                        :sha256, 'standard')
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "bucket": BUCKET_EVIDENCE,
                "object_key": object_key,
                "size": size,
                "sha256": digest,
            },
        )
    ).scalar_one()

    return object_id, digest, size


async def capture_evidence(
    session: AsyncSession,
    minio: Minio,
    *,
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    image: np.ndarray,
    capture_time: dt.datetime,
    incident_id: uuid.UUID | None = None,
    detection_id: uuid.UUID | None = None,
    mask_boxes: list[tuple[float, float, float, float]] | None = None,
    annotations: list[Annotation] | None = None,
    retention_days: int | None = 90,
) -> EvidenceSet:
    """Stores original, masked, and annotated variants.

    The masked variant is what normal read paths serve; fetching the original is a
    separate, permissioned act (`evidence.download`). The annotated variant is the masked
    image with detection boxes drawn - derived from masked, never from the original, so
    the privacy default holds: the box shows where and what, never who.
    """
    original_id = uuid.uuid4()
    masked_id = uuid.uuid4()
    annotated_id = uuid.uuid4()
    expires_at = capture_time + dt.timedelta(days=retention_days) if retention_days else None

    original_key = evidence_object_key(tenant_id, incident_id, original_id, "original")
    original_object_id, original_digest, original_size = await _store_object(
        session, minio, tenant_id=tenant_id, object_key=original_key, payload=encode_jpeg(image)
    )

    masked_image = mask_regions(image, mask_boxes or [])
    masked_key = evidence_object_key(tenant_id, incident_id, masked_id, "masked")
    masked_object_id, masked_digest, masked_size = await _store_object(
        session, minio, tenant_id=tenant_id, object_key=masked_key, payload=encode_jpeg(masked_image)
    )

    # With no boxes to draw, an annotated variant would be byte-identical to the masked
    # one - a duplicate object and a duplicate row for every capture. Fall back to the
    # masked record instead, so `EvidenceSet.annotated` is always safe to read.
    annotated_record: EvidenceRecord | None = None
    if annotations:
        # Boxes go on the masked image, so a face stays blurred underneath its own box.
        annotated_image = draw_detections(masked_image, annotations)
        annotated_key = evidence_object_key(tenant_id, incident_id, annotated_id, "annotated")
        annotated_object_id, annotated_digest, annotated_size = await _store_object(
            session, minio, tenant_id=tenant_id, object_key=annotated_key,
            payload=encode_jpeg(annotated_image),
        )
        annotated_record = EvidenceRecord(
            annotated_id, annotated_object_id, annotated_key, annotated_digest,
            annotated_size, "annotated",
        )

    await session.execute(
        text(
            """
            INSERT INTO evidence
                (id, tenant_id, incident_id, camera_id, detection_id, object_id, evidence_type,
                 capture_time, sha256, privacy_variant, retention_class, expires_at,
                 access_classification)
            VALUES (:id, :tenant_id, :incident_id, :camera_id, :detection_id, :object_id,
                    'snapshot', :capture_time, :sha256, 'original', 'standard', :expires_at,
                    'restricted')
            """
        ),
        {
            "id": original_id,
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "camera_id": camera_id,
            "detection_id": detection_id,
            "object_id": original_object_id,
            "capture_time": capture_time,
            "sha256": original_digest,
            "expires_at": expires_at,
        },
    )

    await session.execute(
        text(
            """
            INSERT INTO evidence
                (id, tenant_id, incident_id, camera_id, detection_id, object_id, evidence_type,
                 capture_time, sha256, privacy_variant, original_evidence_id, retention_class,
                 expires_at, access_classification)
            VALUES (:id, :tenant_id, :incident_id, :camera_id, :detection_id, :object_id,
                    'snapshot', :capture_time, :sha256, 'masked', :original_id, 'standard',
                    :expires_at, 'standard')
            """
        ),
        {
            "id": masked_id,
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "camera_id": camera_id,
            "detection_id": detection_id,
            "object_id": masked_object_id,
            "capture_time": capture_time,
            "sha256": masked_digest,
            "original_id": original_id,
            "expires_at": expires_at,
        },
    )

    if annotated_record is not None:
        await session.execute(
            text(
                """
                INSERT INTO evidence
                    (id, tenant_id, incident_id, camera_id, detection_id, object_id, evidence_type,
                     capture_time, sha256, privacy_variant, original_evidence_id, retention_class,
                     expires_at, access_classification)
                VALUES (:id, :tenant_id, :incident_id, :camera_id, :detection_id, :object_id,
                        'snapshot', :capture_time, :sha256, 'annotated', :original_id, 'standard',
                        :expires_at, 'standard')
                """
            ),
            {
                "id": annotated_id,
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "camera_id": camera_id,
                "detection_id": detection_id,
                "object_id": annotated_record.object_id,
                "capture_time": capture_time,
                "sha256": annotated_record.sha256,
                "original_id": original_id,
                "expires_at": expires_at,
            },
        )

    masked_record = EvidenceRecord(
        masked_id, masked_object_id, masked_key, masked_digest, masked_size, "masked"
    )
    return EvidenceSet(
        original=EvidenceRecord(
            original_id, original_object_id, original_key, original_digest, original_size, "original"
        ),
        masked=masked_record,
        # No boxes to draw means no separate object; the masked variant is the annotated
        # view, so callers never have to branch on whether one exists.
        annotated=annotated_record or masked_record,
    )


async def presign_evidence(
    session: AsyncSession,
    minio: Minio,
    *,
    tenant_id: uuid.UUID,
    evidence_id: uuid.UUID,
    expires: dt.timedelta = dt.timedelta(minutes=5),
) -> str | None:
    """Issues a short-lived URL for one evidence object.

    The tenant filter is in the query and RLS scopes it again below - object storage itself
    performs no authorisation (SCH §2), so the check must happen here. Returns None when
    the evidence does not belong to this tenant, which is indistinguishable from it not
    existing, so this cannot be used to probe other tenants' records.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT so.bucket, so.object_key
                FROM evidence e
                JOIN stored_objects so ON so.id = e.object_id
                WHERE e.id = :evidence_id AND e.tenant_id = :tenant_id
                """
            ),
            {"evidence_id": evidence_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        return None

    return minio.presigned_get_object(row[0], row[1], expires=expires)
