"""Detection persistence in MongoDB (docs/05_BACKEND_SCHEMA.md §9.1).

Detections are immutable observations and arrive at high volume, which is why they live in
MongoDB rather than PostgreSQL (SCH §2). Two properties matter:

**Idempotency.** Edge devices retry, event delivery is at-least-once (TRD §11.3), and an
offline device replays its whole spool on reconnect. `(tenant_id, source_event_id)` is
uniquely indexed, so a redelivered detection is recorded once no matter how many times it
arrives. `record_detection` reports whether the write was new, which is how the caller
avoids incrementing an incident's count twice for the same observation.

**Five distinct timestamps.** TRD-DATA-005 requires distinguishing source capture, edge
receive, cloud receive and processing time. They diverge in exactly the cases that matter:
a device with a skewed clock, or one that spooled offline for six hours and uploaded a
backlog. Collapsing them into one field makes those situations undiagnosable, and makes
"when did this actually happen" unanswerable during an investigation.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

COLLECTION = "detections"

# Retention default. Detections are high-volume telemetry, not business records - the
# incident is the durable artifact. A lifecycle worker honours tenant policy and legal
# hold; this is only the fallback when no policy applies (SCH §16).
DEFAULT_RETENTION_DAYS = 30


@dataclass
class DetectionDocument:
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

    def to_document(self, *, cloud_receive_time: dt.datetime, expires_at: dt.datetime | None) -> dict:
        return {
            "_id": str(uuid.uuid4()),
            "tenant_id": str(self.tenant_id),
            "site_id": str(self.site_id),
            "camera_id": str(self.camera_id),
            "edge_device_id": str(self.edge_device_id) if self.edge_device_id else None,
            "pipeline_version_id": str(self.pipeline_version_id) if self.pipeline_version_id else None,
            "model_version_id": str(self.model_version_id) if self.model_version_id else None,
            "event_type": self.event_type,
            "source_event_id": self.source_event_id,
            "capture_time": self.capture_time,
            "edge_receive_time": self.edge_receive_time,
            "cloud_receive_time": cloud_receive_time,
            "confidence": round(float(self.confidence), 5),
            "objects": self.objects,
            "roi_id": self.roi_id,
            "rule_results": self.rule_results,
            "evidence_refs": self.evidence_refs,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "schema_version": self.schema_version,
            "expires_at": expires_at,
        }


@dataclass(frozen=True)
class RecordResult:
    detection_id: str
    created: bool  # False when this source_event_id had already been recorded


async def ensure_indexes(db: AsyncIOMotorDatabase) -> list[str]:
    """Creates the indexes from SCH §9.1. Idempotent - safe to call at startup.

    Every compound index begins with `tenant_id` (SCH §17), so tenant-scoped queries stay
    selective as the collection grows and no query can accidentally scan across tenants.
    """
    collection = db[COLLECTION]
    created = []

    created.append(
        await collection.create_index(
            [("tenant_id", ASCENDING), ("source_event_id", ASCENDING)],
            unique=True,
            name="uq_tenant_source_event",
        )
    )
    created.append(
        await collection.create_index(
            [("tenant_id", ASCENDING), ("camera_id", ASCENDING), ("capture_time", DESCENDING)],
            name="ix_tenant_camera_capture",
        )
    )
    created.append(
        await collection.create_index(
            [("tenant_id", ASCENDING), ("event_type", ASCENDING), ("capture_time", DESCENDING)],
            name="ix_tenant_type_capture",
        )
    )
    created.append(
        await collection.create_index(
            [("tenant_id", ASCENDING), ("correlation_id", ASCENDING)],
            name="ix_tenant_correlation",
        )
    )
    # TTL index. SCH §9.1 permits this only where retention and legal-hold workflow allow;
    # documents under hold are given a null expires_at so the TTL monitor skips them.
    created.append(
        await collection.create_index(
            "expires_at", expireAfterSeconds=0, name="ttl_expires_at",
        )
    )
    return created


async def record_detection(
    db: AsyncIOMotorDatabase,
    detection: DetectionDocument,
    *,
    retention_days: int | None = DEFAULT_RETENTION_DAYS,
    now: dt.datetime | None = None,
) -> RecordResult:
    """Persists a detection, exactly once per source_event_id.

    Returns `created=False` for a redelivery, along with the id of the document already
    stored, so the caller can link the existing detection to an incident rather than
    treating the duplicate as a new observation.
    """
    cloud_receive_time = now or dt.datetime.now(dt.UTC)
    expires_at = (
        cloud_receive_time + dt.timedelta(days=retention_days) if retention_days else None
    )
    document = detection.to_document(cloud_receive_time=cloud_receive_time, expires_at=expires_at)

    try:
        await db[COLLECTION].insert_one(document)
        return RecordResult(document["_id"], created=True)
    except DuplicateKeyError:
        # Already recorded. Return the original id so callers converge on one document.
        existing = await db[COLLECTION].find_one(
            {"tenant_id": str(detection.tenant_id), "source_event_id": detection.source_event_id},
            {"_id": 1},
        )
        if existing is None:  # pragma: no cover - only under a concurrent delete
            raise
        return RecordResult(existing["_id"], created=False)


async def find_detection(
    db: AsyncIOMotorDatabase, *, tenant_id: uuid.UUID, detection_id: str
) -> dict | None:
    """Tenant id is part of the filter, never inferred from the opaque document id
    (SCH §2 rule 9)."""
    return await db[COLLECTION].find_one({"_id": detection_id, "tenant_id": str(tenant_id)})


async def list_detections_for_camera(
    db: AsyncIOMotorDatabase,
    *,
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    since: dt.datetime | None = None,
    limit: int = 100,
) -> list[dict]:
    query: dict = {"tenant_id": str(tenant_id), "camera_id": str(camera_id)}
    if since:
        query["capture_time"] = {"$gte": since}
    cursor = db[COLLECTION].find(query).sort("capture_time", DESCENDING).limit(limit)
    return await cursor.to_list(length=limit)


async def attach_evidence_ref(
    db: AsyncIOMotorDatabase, *, tenant_id: uuid.UUID, detection_id: str, evidence_id: str
) -> None:
    """Links stored evidence back to the detection that produced it.

    `$addToSet` rather than `$push`: evidence capture can be retried, and a detection
    should not accumulate duplicate references to the same snapshot.
    """
    await db[COLLECTION].update_one(
        {"_id": detection_id, "tenant_id": str(tenant_id)},
        {"$addToSet": {"evidence_refs": evidence_id}},
    )
