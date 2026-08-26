"""Detection persistence in MongoDB.

The property that matters: an edge device that retries, or replays a spool after being
offline, must not multiply detections. Idempotency is enforced by a unique index on
(tenant_id, source_event_id), so it holds regardless of how the caller behaves.

Needs a running MongoDB (TEST_MONGO_DSN); skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from csense_shared.pipeline.detections import (
    COLLECTION,
    DetectionDocument,
    attach_evidence_ref,
    ensure_indexes,
    find_detection,
    list_detections_for_camera,
    record_detection,
)

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TEST_MONGO_DSN"), reason="TEST_MONGO_DSN not set - skipping"
    ),
    pytest.mark.asyncio,
]

CAPTURED_AT = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture()
async def mongo_db():
    """A throwaway database per test run, dropped afterwards."""
    client = AsyncIOMotorClient(os.environ["TEST_MONGO_DSN"], uuidRepresentation="standard")
    name = f"csense_test_{uuid.uuid4().hex[:10]}"
    db = client[name]
    await ensure_indexes(db)
    yield db
    await client.drop_database(name)
    client.close()


def make_detection(tenant_id: uuid.UUID, camera_id: uuid.UUID, source_event_id: str) -> DetectionDocument:
    return DetectionDocument(
        tenant_id=tenant_id,
        site_id=uuid.uuid4(),
        camera_id=camera_id,
        event_type="person.restricted_zone",
        source_event_id=source_event_id,
        capture_time=CAPTURED_AT,
        confidence=0.87,
        objects=[{"class": "person", "bbox": [0.1, 0.2, 0.3, 0.4], "track_id": "12"}],
    )


async def test_detection_is_persisted(mongo_db):
    tenant_id, camera_id = uuid.uuid4(), uuid.uuid4()
    result = await record_detection(mongo_db, make_detection(tenant_id, camera_id, "edge-1"))

    assert result.created is True
    stored = await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert stored["event_type"] == "person.restricted_zone"
    assert stored["objects"][0]["class"] == "person"


async def test_redelivered_detection_is_recorded_once(mongo_db):
    """At-least-once delivery, edge retries, and offline spool replay all cause this."""
    tenant_id, camera_id = uuid.uuid4(), uuid.uuid4()

    first = await record_detection(mongo_db, make_detection(tenant_id, camera_id, "edge-1"))
    second = await record_detection(mongo_db, make_detection(tenant_id, camera_id, "edge-1"))
    third = await record_detection(mongo_db, make_detection(tenant_id, camera_id, "edge-1"))

    assert first.created is True
    assert second.created is False and third.created is False
    # All three converge on the same document, so callers link to one detection.
    assert first.detection_id == second.detection_id == third.detection_id

    count = await mongo_db[COLLECTION].count_documents({"tenant_id": str(tenant_id)})
    assert count == 1


async def test_same_source_id_in_different_tenants_is_not_a_collision(mongo_db):
    """Edge devices choose their own event ids; two tenants can legitimately produce the
    same string. Uniqueness is per tenant, not global."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    camera = uuid.uuid4()

    a = await record_detection(mongo_db, make_detection(tenant_a, camera, "frame-001"))
    b = await record_detection(mongo_db, make_detection(tenant_b, camera, "frame-001"))

    assert a.created and b.created
    assert a.detection_id != b.detection_id


async def test_lookup_requires_the_owning_tenant(mongo_db):
    """SCH §2 rule 9: MongoDB is never trusted to infer tenant ownership from an opaque
    document id."""
    tenant_id, other_tenant = uuid.uuid4(), uuid.uuid4()
    result = await record_detection(mongo_db, make_detection(tenant_id, uuid.uuid4(), "edge-1"))

    assert await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert await find_detection(mongo_db, tenant_id=other_tenant, detection_id=result.detection_id) is None


async def test_five_timestamps_are_distinguished(mongo_db):
    """TRD-DATA-005. These diverge when a device has a skewed clock or replays a backlog,
    which is exactly when an investigator needs to tell them apart."""
    tenant_id = uuid.uuid4()
    detection = make_detection(tenant_id, uuid.uuid4(), "edge-1")
    detection.edge_receive_time = CAPTURED_AT + dt.timedelta(milliseconds=120)

    cloud_time = CAPTURED_AT + dt.timedelta(hours=6)  # a spool replayed after 6h offline
    result = await record_detection(mongo_db, detection, now=cloud_time)

    stored = await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert stored["capture_time"].replace(tzinfo=dt.UTC) == CAPTURED_AT
    assert stored["edge_receive_time"] is not None
    assert stored["cloud_receive_time"].replace(tzinfo=dt.UTC) == cloud_time
    # The six-hour gap must remain visible, not be collapsed into one field.
    assert stored["cloud_receive_time"] > stored["capture_time"]


async def test_retention_sets_an_expiry(mongo_db):
    tenant_id = uuid.uuid4()
    result = await record_detection(
        mongo_db, make_detection(tenant_id, uuid.uuid4(), "edge-1"), retention_days=7, now=CAPTURED_AT
    )
    stored = await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert stored["expires_at"].replace(tzinfo=dt.UTC) == CAPTURED_AT + dt.timedelta(days=7)


async def test_legal_hold_style_null_retention_never_expires(mongo_db):
    """A null expires_at is how a document is kept out of the TTL monitor's reach."""
    tenant_id = uuid.uuid4()
    result = await record_detection(
        mongo_db, make_detection(tenant_id, uuid.uuid4(), "edge-1"), retention_days=None
    )
    stored = await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert stored["expires_at"] is None


async def test_camera_history_is_tenant_scoped_and_ordered(mongo_db):
    tenant_id, camera_id = uuid.uuid4(), uuid.uuid4()
    for index in range(5):
        detection = make_detection(tenant_id, camera_id, f"edge-{index}")
        detection.capture_time = CAPTURED_AT + dt.timedelta(seconds=index)
        await record_detection(mongo_db, detection)

    # Another tenant's detection on a coincidentally identical camera id must not appear.
    await record_detection(mongo_db, make_detection(uuid.uuid4(), camera_id, "other-1"))

    rows = await list_detections_for_camera(mongo_db, tenant_id=tenant_id, camera_id=camera_id)
    assert len(rows) == 5
    assert rows[0]["capture_time"] > rows[-1]["capture_time"], "newest first"


async def test_evidence_refs_do_not_duplicate_on_retry(mongo_db):
    """Evidence capture can be retried; the detection should not accumulate duplicates."""
    tenant_id = uuid.uuid4()
    result = await record_detection(mongo_db, make_detection(tenant_id, uuid.uuid4(), "edge-1"))
    evidence_id = str(uuid.uuid4())

    for _ in range(3):
        await attach_evidence_ref(
            mongo_db, tenant_id=tenant_id, detection_id=result.detection_id, evidence_id=evidence_id
        )

    stored = await find_detection(mongo_db, tenant_id=tenant_id, detection_id=result.detection_id)
    assert stored["evidence_refs"] == [evidence_id]


async def test_indexes_are_created_and_idempotent(mongo_db):
    # ensure_indexes already ran in the fixture; running again must not fail.
    await ensure_indexes(mongo_db)
    names = set((await mongo_db[COLLECTION].index_information()).keys())
    assert {"uq_tenant_source_event", "ix_tenant_camera_capture", "ttl_expires_at"} <= names
