"""Incident creation, deduplication, and lifecycle against a real database.

The property under test that matters most: a rule firing on many consecutive frames must
produce ONE incident, not one per frame. Everything else in the alerting chain -
notification volume, escalation, operator trust - depends on it.

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

from csense_shared.pipeline.incidents import (
    InvalidTransitionError,
    transition_incident,
    upsert_incident_from_match,
)
from csense_shared.pipeline.rules import DetectedObject, Rule

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
    ),
    pytest.mark.asyncio,
]

CAPTURED_AT = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)
RULE = Rule(
    type_code="zone.intrusion",
    alertable_classes=frozenset({"person"}),
    severity="high",
    cooldown_seconds=300,
)
PERSON = DetectedObject(class_name="person", confidence=0.91, bbox=(0.4, 0.4, 0.6, 0.6))


def _async_dsn() -> str:
    """The sync DSN the other tests use, converted for asyncpg."""
    raw = os.environ["TEST_POSTGRES_DSN"]
    parts = dict(p.split("=", 1) for p in raw.split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest_asyncio.fixture()
async def tenant_session():
    """A tenant-scoped session plus a site and camera to attach incidents to.

    Runs as the owner role (which the fixture needs for setup), but sets `app.tenant_id`
    so the same RLS path the application uses is exercised.
    """
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
            org_id = (
                await session.execute(
                    text(
                        "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                        "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                    ),
                    {"n": f"Pipeline Test {suffix}", "s": f"pipeline-test-{suffix}"},
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
                    text(
                        "INSERT INTO sites (tenant_id, name, code) VALUES (:t, 'Test Site', :c) RETURNING id"
                    ),
                    {"t": tenant_id, "c": f"site-{suffix}"},
                )
            ).scalar_one()
            camera_id = (
                await session.execute(
                    text(
                        "INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                        "VALUES (:t, :s, 'Test Camera', :c, 'ready') RETURNING id"
                    ),
                    {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
                )
            ).scalar_one()

    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            await session.execute(text("SELECT set_config('app.is_platform', 'false', true)"))
            yield {
                "session": session,
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
            }
            # Roll back so each test leaves no residue.
            await session.rollback()

    await engine.dispose()


async def _upsert(ctx, *, detection_id: str, captured_at=CAPTURED_AT, rule=RULE, detected=PERSON):
    return await upsert_incident_from_match(
        ctx["session"],
        tenant_id=ctx["tenant_id"],
        site_id=ctx["site_id"],
        camera_id=ctx["camera_id"],
        rule=rule,
        detected=detected,
        detection_id=detection_id,
        captured_at=captured_at,
        zone_id="zone-a",
    )


async def test_first_detection_creates_an_incident(tenant_session):
    record = await _upsert(tenant_session, detection_id="det-1")

    assert record.created is True
    assert record.incident_number == 1
    assert record.status == "open"
    assert record.detection_count == 1


async def test_repeated_detections_produce_one_incident(tenant_session):
    """The core deduplication guarantee: 25 consecutive frames, one incident."""
    records = []
    for frame in range(25):
        records.append(
            await _upsert(
                tenant_session,
                detection_id=f"det-{frame}",
                captured_at=CAPTURED_AT + dt.timedelta(seconds=frame),
            )
        )

    assert records[0].created is True
    assert all(r.created is False for r in records[1:]), "only the first frame should create"
    assert len({r.id for r in records}) == 1, "all frames must fold into one incident"
    assert records[-1].detection_count == 25

    count = (
        await tenant_session["session"].execute(
            text("SELECT count(*) FROM incidents WHERE tenant_id = :t"),
            {"t": tenant_session["tenant_id"]},
        )
    ).scalar_one()
    assert count == 1


async def test_every_detection_is_linked_to_the_incident(tenant_session):
    """Folding frames together must not lose the evidence trail."""
    for frame in range(5):
        await _upsert(tenant_session, detection_id=f"det-{frame}")

    links = (
        await tenant_session["session"].execute(
            text("SELECT count(*) FROM incident_detection_links WHERE tenant_id = :t"),
            {"t": tenant_session["tenant_id"]},
        )
    ).scalar_one()
    assert links == 5


async def test_redelivered_detection_is_idempotent(tenant_session):
    """At-least-once delivery means the same detection will arrive twice."""
    await _upsert(tenant_session, detection_id="det-1")
    await _upsert(tenant_session, detection_id="det-1")

    links = (
        await tenant_session["session"].execute(
            text("SELECT count(*) FROM incident_detection_links WHERE tenant_id = :t"),
            {"t": tenant_session["tenant_id"]},
        )
    ).scalar_one()
    assert links == 1, "the same detection must not be linked twice"


async def test_different_classes_create_separate_incidents(tenant_session):
    rule = Rule(type_code="kitchen.ppe", alertable_classes=frozenset({"no_glove", "maskoff"}))
    a = await _upsert(
        tenant_session, detection_id="d1", rule=rule,
        detected=DetectedObject("no_glove", 0.9, (0.4, 0.4, 0.6, 0.6)),
    )
    b = await _upsert(
        tenant_session, detection_id="d2", rule=rule,
        detected=DetectedObject("maskoff", 0.9, (0.4, 0.4, 0.6, 0.6)),
    )

    assert a.id != b.id
    assert a.created and b.created


async def test_incident_numbers_are_sequential_per_tenant(tenant_session):
    rule_a = Rule(type_code="type.a", alertable_classes=frozenset({"person"}))
    rule_b = Rule(type_code="type.b", alertable_classes=frozenset({"person"}))
    first = await _upsert(tenant_session, detection_id="d1", rule=rule_a)
    second = await _upsert(tenant_session, detection_id="d2", rule=rule_b)

    assert (first.incident_number, second.incident_number) == (1, 2)


async def test_resolved_incident_allows_a_new_one_for_the_same_key(tenant_session):
    """A recurrence after resolution is a new occurrence, not a reopening - each gets its
    own history."""
    first = await _upsert(tenant_session, detection_id="d1")
    await transition_incident(
        tenant_session["session"],
        tenant_id=tenant_session["tenant_id"],
        incident_id=first.id,
        new_status="resolved",
        actor_type="user",
        actor_id=str(uuid.uuid4()),
        resolution_code="false_positive",
    )

    second = await _upsert(tenant_session, detection_id="d2")
    assert second.created is True
    assert second.id != first.id


async def test_state_machine_rejects_illegal_transitions(tenant_session):
    record = await _upsert(tenant_session, detection_id="d1")
    await transition_incident(
        tenant_session["session"],
        tenant_id=tenant_session["tenant_id"],
        incident_id=record.id,
        new_status="resolved",
        actor_type="user",
        actor_id=str(uuid.uuid4()),
    )

    with pytest.raises(InvalidTransitionError):
        await transition_incident(
            tenant_session["session"],
            tenant_id=tenant_session["tenant_id"],
            incident_id=record.id,
            new_status="acknowledged",
            actor_type="user",
            actor_id=str(uuid.uuid4()),
        )


async def test_lifecycle_is_recorded_as_append_only_history(tenant_session):
    record = await _upsert(tenant_session, detection_id="d1")
    actor = str(uuid.uuid4())
    for status in ("acknowledged", "investigating", "resolved"):
        await transition_incident(
            tenant_session["session"],
            tenant_id=tenant_session["tenant_id"],
            incident_id=record.id,
            new_status=status,
            actor_type="user",
            actor_id=actor,
            reason=f"moving to {status}",
        )

    rows = (
        await tenant_session["session"].execute(
            text(
                "SELECT event_type, previous_status, new_status FROM incident_events "
                "WHERE incident_id = :i ORDER BY occurred_at, event_type"
            ),
            {"i": record.id},
        )
    ).all()

    events = {r[0] for r in rows}
    assert "incident.created" in events
    assert {"incident.acknowledged", "incident.investigating", "incident.resolved"} <= events

    transitions = [(r[1], r[2]) for r in rows if r[1] is not None]
    assert ("open", "acknowledged") in transitions
    assert ("investigating", "resolved") in transitions


async def test_acknowledging_stamps_who_and_when(tenant_session):
    record = await _upsert(tenant_session, detection_id="d1")
    actor = uuid.uuid4()
    await transition_incident(
        tenant_session["session"],
        tenant_id=tenant_session["tenant_id"],
        incident_id=record.id,
        new_status="acknowledged",
        actor_type="user",
        actor_id=str(actor),
    )

    row = (
        await tenant_session["session"].execute(
            text("SELECT acknowledged_at, acknowledged_by FROM incidents WHERE id = :i"),
            {"i": record.id},
        )
    ).first()
    assert row[0] is not None
    assert row[1] == actor
