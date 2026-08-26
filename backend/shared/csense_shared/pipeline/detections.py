"""Detection persistence in PostgreSQL.

Detections were originally assigned to MongoDB by docs/05_BACKEND_SCHEMA.md §2. They live
in PostgreSQL instead - see migration 0012 for the reasoning. The short version: tenant
isolation is now enforced by row-level security rather than by an application wrapper,
detections and incidents commit in one transaction with a real foreign key between them,
and the system operates one datastore instead of two.

Two properties carry over unchanged:

**Idempotency.** Edge devices retry, event delivery is at-least-once (TRD §11.3), and a
device that has been offline replays its whole spool on reconnect. A unique constraint on
`(tenant_id, source_event_id)` means a redelivered detection is stored once however many
times it arrives. `record_detection` reports whether the write was new, so a caller does
not increment an incident's count twice for one observation.

**Five distinct timestamps** (TRD-DATA-005): source capture, edge receive, cloud receive.
They diverge exactly when it matters - a device with a skewed clock, or a six-hour backlog
uploaded at once - and collapsing them makes "when did this actually happen" unanswerable
during an investigation.

Retention differs from the MongoDB version: PostgreSQL has no TTL index, so `expires_at`
is swept by `delete_expired_detections()` on a schedule rather than by the storage engine.
A null `expires_at` means never expire, which is how legal hold keeps rows out of reach.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Detections are high-volume telemetry, not business records - the incident is the durable
# artifact. A lifecycle worker honours tenant policy and legal hold; this is only the
# fallback when no policy applies (SCH §16).
DEFAULT_RETENTION_DAYS = 30


@dataclass
class DetectionDocument:
    """One immutable observation produced by a pipeline run."""

    tenant_id: uuid.UUID
    site_id: uuid.UUID
    camera_id: uuid.UUID
    event_type: str
    # Stable id from the producing device/runtime. This is the idempotency key.
    source_event_id: str
    capture_time: dt.datetime
    confidence: float
    objects: list[dict]
    pipeline_version_id: uuid.UUID | None = None
    model_version_id: uuid.UUID | None = None
    edge_device_id: uuid.UUID | None = None
    edge_receive_time: dt.datetime | None = None
    roi_id: str | None = None
    rule_results: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    correlation_id: uuid.UUID | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class RecordResult:
    detection_id: uuid.UUID
    created: bool  # False when this source_event_id had already been recorded


async def record_detection(
    session: AsyncSession,
    detection: DetectionDocument,
    *,
    retention_days: int | None = DEFAULT_RETENTION_DAYS,
    now: dt.datetime | None = None,
) -> RecordResult:
    """Persists a detection, exactly once per (tenant, source_event_id).

    `ON CONFLICT DO NOTHING` plus a follow-up select rather than `DO UPDATE`: a detection
    is an immutable observation, so a redelivery must not overwrite what was originally
    recorded. Returns the existing row's id so callers converge on one detection.
    """
    cloud_receive_time = now or dt.datetime.now(dt.UTC)
    expires_at = (
        cloud_receive_time + dt.timedelta(days=retention_days) if retention_days else None
    )

    params = {
        "tenant_id": detection.tenant_id,
        "site_id": detection.site_id,
        "camera_id": detection.camera_id,
        "edge_device_id": detection.edge_device_id,
        "pipeline_version_id": detection.pipeline_version_id,
        "model_version_id": detection.model_version_id,
        "event_type": detection.event_type,
        "source_event_id": detection.source_event_id,
        "capture_time": detection.capture_time,
        "edge_receive_time": detection.edge_receive_time,
        "cloud_receive_time": cloud_receive_time,
        "confidence": round(float(detection.confidence), 5),
        "objects": json.dumps(detection.objects),
        "roi_id": detection.roi_id,
        "rule_results": json.dumps(detection.rule_results),
        "evidence_refs": json.dumps(detection.evidence_refs),
        "correlation_id": detection.correlation_id,
        "schema_version": detection.schema_version,
        "expires_at": expires_at,
    }

    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO detections (
                    tenant_id, site_id, camera_id, edge_device_id, pipeline_version_id,
                    model_version_id, event_type, source_event_id, capture_time,
                    edge_receive_time, cloud_receive_time, confidence, objects, roi_id,
                    rule_results, evidence_refs, correlation_id, schema_version, expires_at
                )
                VALUES (
                    :tenant_id, :site_id, :camera_id, :edge_device_id, :pipeline_version_id,
                    :model_version_id, :event_type, :source_event_id, :capture_time,
                    :edge_receive_time, :cloud_receive_time, :confidence,
                    CAST(:objects AS jsonb), :roi_id, CAST(:rule_results AS jsonb),
                    CAST(:evidence_refs AS jsonb), :correlation_id, :schema_version, :expires_at
                )
                ON CONFLICT (tenant_id, source_event_id) DO NOTHING
                RETURNING id
                """
            ),
            params,
        )
    ).scalar_one_or_none()

    if inserted is not None:
        return RecordResult(inserted, created=True)

    existing = (
        await session.execute(
            text(
                "SELECT id FROM detections WHERE tenant_id = :tenant_id "
                "AND source_event_id = :source_event_id"
            ),
            {
                "tenant_id": detection.tenant_id,
                "source_event_id": detection.source_event_id,
            },
        )
    ).scalar_one()
    return RecordResult(existing, created=False)


async def find_detection(
    session: AsyncSession, *, tenant_id: uuid.UUID, detection_id: uuid.UUID
) -> dict | None:
    """Row-level security already scopes this; the explicit tenant predicate is defence in
    depth, and makes the intent readable at the call site."""
    row = (
        await session.execute(
            text(
                """
                SELECT id, tenant_id, site_id, camera_id, event_type, source_event_id,
                       capture_time, edge_receive_time, cloud_receive_time, confidence,
                       objects, roi_id, rule_results, evidence_refs, correlation_id,
                       schema_version, expires_at
                FROM detections
                WHERE id = :detection_id AND tenant_id = :tenant_id
                """
            ),
            {"detection_id": detection_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_detections_for_camera(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    since: dt.datetime | None = None,
    limit: int = 100,
) -> list[dict]:
    clause = "AND capture_time >= :since" if since else ""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT id, camera_id, event_type, source_event_id, capture_time,
                       confidence, objects, roi_id, evidence_refs
                FROM detections
                WHERE tenant_id = :tenant_id AND camera_id = :camera_id {clause}
                ORDER BY capture_time DESC
                LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "camera_id": camera_id, "since": since, "limit": limit},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def attach_evidence_ref(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    detection_id: uuid.UUID,
    evidence_id: str,
) -> None:
    """Links stored evidence back to the detection that produced it.

    Appends only when absent: evidence capture can be retried, and a detection should not
    accumulate duplicate references to the same snapshot.
    """
    await session.execute(
        text(
            """
            UPDATE detections
            SET evidence_refs = CASE
                WHEN evidence_refs @> CAST(:ref_json AS jsonb) THEN evidence_refs
                ELSE evidence_refs || CAST(:ref_json AS jsonb)
            END
            WHERE id = :detection_id AND tenant_id = :tenant_id
            """
        ),
        {
            "detection_id": detection_id,
            "tenant_id": tenant_id,
            "ref_json": json.dumps([evidence_id]),
        },
    )


async def delete_expired_detections(
    session: AsyncSession, *, batch_size: int = 10_000, now: dt.datetime | None = None
) -> int:
    """Retention sweep - the replacement for MongoDB's TTL index.

    Batched so a large backlog cannot hold a long transaction or lock the table against
    ingestion. Rows with a null `expires_at` are never selected, which is how legal hold
    and indefinite-retention policies keep data out of reach.

    Runs as a platform-scoped job across all tenants, so the caller must use a session
    with platform privileges.
    """
    deleted = (
        await session.execute(
            text(
                """
                WITH doomed AS (
                    SELECT id FROM detections
                    WHERE expires_at IS NOT NULL AND expires_at <= :now
                    LIMIT :batch_size
                )
                DELETE FROM detections d USING doomed
                WHERE d.id = doomed.id
                RETURNING d.id
                """
            ),
            {"now": now or dt.datetime.now(dt.UTC), "batch_size": batch_size},
        )
    ).all()
    return len(deleted)
