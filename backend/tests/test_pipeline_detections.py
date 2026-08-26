"""Detection persistence in PostgreSQL.

Detections moved out of MongoDB (migration 0012). The idempotency and timestamp
guarantees are unchanged, and two new ones are now testable that were not before:
tenant isolation is enforced by row-level security rather than by an application filter,
and a detection cannot be deleted while an incident still links to it.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.pipeline.detections import (
    DetectionDocument,
    attach_evidence_ref,
    delete_expired_detections,
    find_detection,
    list_detections_for_camera,
    record_detection,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

CAPTURED_AT = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)


def _async_dsn(env_var: str = "TEST_POSTGRES_DSN") -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _make_tenant(session, suffix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    org_id = (
        await session.execute(
            text(
                "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
            ),
            {"n": f"Detection Test {suffix}", "s": f"detection-test-{suffix}"},
        )
    ).scalar_one()
    tenant_id = (
        await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
            {"o": org_id},
        )
    ).scalar_one()
    site_id = (
        await session.execute(
            text("INSERT INTO sites (tenant_id, name, code) VALUES (:t, 'S', :c) RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}"},
        )
    ).scalar_one()
    camera_id = (
        await session.execute(
            text(
                "INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                "VALUES (:t, :s, 'C', :c, 'ready') RETURNING id"
            ),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )
    ).scalar_one()
    return tenant_id, site_id, camera_id


@pytest_asyncio.fixture()
async def ctx():
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        tenant_id, site_id, camera_id = await _make_tenant(session, suffix)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
        await session.execute(text("SELECT set_config('app.is_platform', 'false', true)"))
        yield {"session": session, "tenant_id": tenant_id, "site_id": site_id, "camera_id": camera_id}
        await session.rollback()

    await engine.dispose()


def make_detection(ctx, source_event_id: str, **overrides) -> DetectionDocument:
    params = {
        "tenant_id": ctx["tenant_id"],
        "site_id": ctx["site_id"],
        "camera_id": ctx["camera_id"],
        "event_type": "person.restricted_zone",
        "source_event_id": source_event_id,
        "capture_time": CAPTURED_AT,
        "confidence": 0.87,
        "objects": [{"class": "person", "bbox": [0.1, 0.2, 0.3, 0.4], "track_id": "12"}],
    }
    params.update(overrides)
    return DetectionDocument(**params)


async def test_detection_is_persisted(ctx):
    result = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))

    assert result.created is True
    stored = await find_detection(
        ctx["session"], tenant_id=ctx["tenant_id"], detection_id=result.detection_id
    )
    assert stored["event_type"] == "person.restricted_zone"
    assert stored["objects"][0]["class"] == "person"


async def test_redelivered_detection_is_recorded_once(ctx):
    """At-least-once delivery, edge retries, and offline spool replay all cause this."""
    first = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))
    second = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))
    third = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))

    assert first.created is True
    assert second.created is False and third.created is False
    assert first.detection_id == second.detection_id == third.detection_id

    count = (
        await ctx["session"].execute(
            text("SELECT count(*) FROM detections WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
        )
    ).scalar_one()
    assert count == 1


async def test_redelivery_does_not_overwrite_the_original(ctx):
    """A detection is an immutable observation. A retry carrying different data must not
    silently replace what was first recorded - that would rewrite history."""
    await record_detection(ctx["session"], make_detection(ctx, "edge-1", confidence=0.87))
    await record_detection(
        ctx["session"],
        make_detection(ctx, "edge-1", confidence=0.11, objects=[{"class": "cat"}]),
    )

    row = (
        await ctx["session"].execute(
            text("SELECT confidence, objects FROM detections WHERE source_event_id = 'edge-1'")
        )
    ).first()
    assert float(row[0]) == pytest.approx(0.87)
    assert row[1][0]["class"] == "person"


async def test_five_timestamps_are_distinguished(ctx):
    """TRD-DATA-005. These diverge when a device has a skewed clock or replays a backlog,
    which is exactly when an investigator needs to tell them apart."""
    detection = make_detection(ctx, "edge-1")
    detection.edge_receive_time = CAPTURED_AT + dt.timedelta(milliseconds=120)
    cloud_time = CAPTURED_AT + dt.timedelta(hours=6)  # spool replayed after 6h offline

    result = await record_detection(ctx["session"], detection, now=cloud_time)
    stored = await find_detection(
        ctx["session"], tenant_id=ctx["tenant_id"], detection_id=result.detection_id
    )

    assert stored["capture_time"] == CAPTURED_AT
    assert stored["edge_receive_time"] is not None
    assert stored["cloud_receive_time"] == cloud_time
    # The six-hour gap must stay visible, not be collapsed into one field.
    assert stored["cloud_receive_time"] > stored["capture_time"]


async def test_confidence_outside_range_is_rejected(ctx):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await record_detection(ctx["session"], make_detection(ctx, "edge-bad", confidence=1.5))


async def test_retention_sets_an_expiry(ctx):
    result = await record_detection(
        ctx["session"], make_detection(ctx, "edge-1"), retention_days=7, now=CAPTURED_AT
    )
    stored = await find_detection(
        ctx["session"], tenant_id=ctx["tenant_id"], detection_id=result.detection_id
    )
    assert stored["expires_at"] == CAPTURED_AT + dt.timedelta(days=7)


async def test_null_retention_never_expires(ctx):
    """A null expires_at is how legal hold keeps a row out of the sweeper's reach."""
    result = await record_detection(
        ctx["session"], make_detection(ctx, "edge-1"), retention_days=None
    )
    stored = await find_detection(
        ctx["session"], tenant_id=ctx["tenant_id"], detection_id=result.detection_id
    )
    assert stored["expires_at"] is None


async def test_retention_sweep_deletes_only_expired_rows(ctx):
    """PostgreSQL has no TTL index, so this sweep replaces it. It must not touch rows that
    are unexpired or held indefinitely."""
    await record_detection(ctx["session"], make_detection(ctx, "old"), retention_days=1, now=CAPTURED_AT)
    await record_detection(
        ctx["session"], make_detection(ctx, "fresh"), retention_days=365, now=CAPTURED_AT
    )
    await record_detection(ctx["session"], make_detection(ctx, "held"), retention_days=None)

    # Sweeping needs platform scope; this session is tenant-scoped, so opt in explicitly.
    await ctx["session"].execute(text("SELECT set_config('app.is_platform', 'true', true)"))
    deleted = await delete_expired_detections(
        ctx["session"], now=CAPTURED_AT + dt.timedelta(days=2)
    )
    await ctx["session"].execute(text("SELECT set_config('app.is_platform', 'false', true)"))

    assert deleted == 1
    remaining = (
        await ctx["session"].execute(
            text("SELECT source_event_id FROM detections WHERE tenant_id = :t ORDER BY source_event_id"),
            {"t": ctx["tenant_id"]},
        )
    ).scalars().all()
    assert remaining == ["fresh", "held"]


async def test_camera_history_is_ordered_newest_first(ctx):
    for index in range(5):
        await record_detection(
            ctx["session"],
            make_detection(ctx, f"edge-{index}", capture_time=CAPTURED_AT + dt.timedelta(seconds=index)),
        )

    rows = await list_detections_for_camera(
        ctx["session"], tenant_id=ctx["tenant_id"], camera_id=ctx["camera_id"]
    )
    assert len(rows) == 5
    assert rows[0]["capture_time"] > rows[-1]["capture_time"]


async def test_evidence_refs_do_not_duplicate_on_retry(ctx):
    """Evidence capture can be retried; the detection should not accumulate duplicates."""
    result = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))
    evidence_id = str(uuid.uuid4())

    for _ in range(3):
        await attach_evidence_ref(
            ctx["session"],
            tenant_id=ctx["tenant_id"],
            detection_id=result.detection_id,
            evidence_id=evidence_id,
        )

    stored = await find_detection(
        ctx["session"], tenant_id=ctx["tenant_id"], detection_id=result.detection_id
    )
    assert stored["evidence_refs"] == [evidence_id]


async def test_lookup_requires_the_owning_tenant(ctx):
    result = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))
    assert await find_detection(
        ctx["session"], tenant_id=uuid.uuid4(), detection_id=result.detection_id
    ) is None


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_API_DSN"), reason="TEST_POSTGRES_API_DSN not set"
)
async def test_row_level_security_hides_other_tenants_detections(ctx):
    """The capability MongoDB could not offer: isolation enforced by the database rather
    than by an application filter. Uses the restricted API role, so a bypass would fail
    here even if application code were compromised."""
    result = await record_detection(ctx["session"], make_detection(ctx, "edge-1"))
    await ctx["session"].commit()

    engine = create_async_engine(_async_dsn("TEST_POSTGRES_API_DSN"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            # Scoped to a different tenant, and attempting the platform bypass too.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(uuid.uuid4())}
            )
            await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))

            visible = (
                await session.execute(
                    text("SELECT count(*) FROM detections WHERE id = :id"),
                    {"id": result.detection_id},
                )
            ).scalar_one()
            assert visible == 0, "another tenant must not see this detection"
    finally:
        await engine.dispose()
        # Clean up the committed rows this test had to persist.
        async with async_sessionmaker(create_async_engine(_async_dsn()))() as cleanup, cleanup.begin():
            await cleanup.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
            await cleanup.execute(
                text("DELETE FROM tenants WHERE id = :t"), {"t": ctx["tenant_id"]}
            )
