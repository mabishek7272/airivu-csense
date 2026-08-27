"""Detection ingestion: rule loading and the full chain in one transaction.

The behaviours worth protecting here are the ones that turn a working system into an
untrusted one:

  **A replayed event must not alert twice.** Edge devices buffer and resend after a network
  drop. If a replay opens a second incident, a single intrusion becomes a stream of 3am
  calls and people stop answering them.

  **A continuing incident must not re-arm escalation.** A person walking across a yard
  produces a detection per frame. Only the frame that opens the incident schedules alerts.

  **Bad tenant data must not stop ingestion.** Zone geometry is tenant-editable through the
  CRM. A malformed polygon degrades that rule to whole-frame rather than raising, because
  raising would stop every camera at the site.

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

from csense_shared.pipeline.ingest import (
    MAX_OBJECTS_PER_FRAME,
    _polygon_from_zone,
    _to_site_time,
    ingest_detection,
    load_rules,
)
from csense_shared.pipeline.rules import DetectedObject

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

# 02:00 UTC is 07:30 in Kolkata - deliberately chosen so a timezone mistake changes the
# outcome of the overnight-rule test rather than passing by luck.
NOW = dt.datetime(2026, 8, 27, 2, 0, tzinfo=dt.UTC)

INSIDE = DetectedObject("person", 0.94, (0.44, 0.36, 0.55, 0.92))
OUTSIDE = DetectedObject("person", 0.91, (0.02, 0.36, 0.12, 0.92))
ZONE_POLYGON = '{"polygon":[[0.35,0.30],[1.0,0.30],[1.0,1.0],[0.35,1.0]]}'


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest_asyncio.fixture()
async def ctx():
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Ingest Test {suffix}", "s": f"ingest-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code, timezone) "
                 "VALUES (:t,'Depot',:c,'Asia/Kolkata') RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}"},
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Bay 2',:c,'ready') RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )).scalar_one()
        zone_id = (await session.execute(
            text("INSERT INTO zones (tenant_id, site_id, name, zone_type, geometry_json) "
                 "VALUES (:t,:s,'Dock','restricted', CAST(:g AS jsonb)) RETURNING id"),
            {"t": tenant_id, "s": site_id, "g": ZONE_POLYGON},
        )).scalar_one()
        group_id = (await session.execute(
            text("INSERT INTO recipient_groups (tenant_id, name) VALUES (:t,'On call') "
                 "RETURNING id"),
            {"t": tenant_id},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO recipient_group_members (tenant_id, recipient_group_id, "
                 "display_name, email, channels) "
                 "VALUES (:t,:g,'Guard','guard@example.com', CAST('[\"email\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id},
        )

    yield {
        "factory": factory, "tenant_id": tenant_id, "site_id": site_id,
        "camera_id": camera_id, "zone_id": zone_id, "suffix": suffix,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
        )
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
    await engine.dispose()


async def add_rule(ctx, **overrides) -> None:
    params = {
        "t": ctx["tenant_id"], "s": ctx["site_id"], "c": ctx["camera_id"],
        "z": ctx["zone_id"], "name": "No entry", "type_code": "zone.intrusion",
        "classes": '["person"]', "conf": 0.4, "sev": "high", "overlap": 0.3,
        "from_hour": None, "to_hour": None,
    }
    params.update(overrides)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("INSERT INTO detection_rules (tenant_id, site_id, camera_id, zone_id, name, "
                 "type_code, alertable_classes, min_confidence, severity, min_roi_overlap, "
                 "active_from_hour, active_to_hour) "
                 "VALUES (:t,:s,:c,:z,:name,:type_code, CAST(:classes AS jsonb), :conf, :sev, "
                 ":overlap, :from_hour, :to_hour)"),
            params,
        )


async def run_ingest(ctx, *, objects=(INSIDE,), event_suffix="1", now=NOW, captured_at=None):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return await ingest_detection(
            session,
            None,  # no object store: evidence capture is exercised by the e2e script
            tenant_id=ctx["tenant_id"],
            site_id=ctx["site_id"],
            camera_id=ctx["camera_id"],
            source_event_id=f"test-{ctx['suffix']}-{event_suffix}",
            captured_at=captured_at or now,
            objects=list(objects),
            site_timezone="Asia/Kolkata",
            now=now,
        )


# --- Rule loading ---------------------------------------------------------------------

async def test_camera_and_site_rules_both_load(ctx):
    """A site-wide rule must apply to every camera without being duplicated per camera."""
    await add_rule(ctx, name="Camera rule")
    await add_rule(ctx, name="Site rule", c=None)

    async with ctx["factory"]() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        rules = await load_rules(
            session, tenant_id=ctx["tenant_id"], camera_id=ctx["camera_id"]
        )

    assert len(rules) == 2


async def test_disabled_rules_are_not_loaded(ctx):
    await add_rule(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE detection_rules SET status = 'disabled' WHERE tenant_id = :t"),
            {"t": ctx["tenant_id"]},
        )
        rules = await load_rules(
            session, tenant_id=ctx["tenant_id"], camera_id=ctx["camera_id"]
        )

    assert rules == []


async def test_rule_polygon_comes_from_its_zone(ctx):
    await add_rule(ctx)
    async with ctx["factory"]() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        (rule, zone_id), = await load_rules(
            session, tenant_id=ctx["tenant_id"], camera_id=ctx["camera_id"]
        )

    assert zone_id == ctx["zone_id"]
    assert rule.roi_polygon == ((0.35, 0.3), (1.0, 0.3), (1.0, 1.0), (0.35, 1.0))


# --- Zone geometry is tenant-editable and must never raise -----------------------------

@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {},
        {"polygon": "not a list"},
        {"polygon": [[0.1, 0.1], [0.9, 0.1]]},          # only two points
        {"polygon": [[0.1, "x"], [0.9, 0.1], [0.5, 1]]},  # non-numeric
        {"points": [{"x": 0.1}, {"x": 0.9, "y": 0.1}]},   # missing key
    ],
)
def test_unusable_geometry_becomes_whole_frame(geometry):
    assert _polygon_from_zone(geometry) is None


def test_both_point_shapes_are_accepted():
    """Zone geometry has been written both ways; neither should be a surprise."""
    assert _polygon_from_zone({"polygon": [[0, 0], [1, 0], [1, 1]]}) == (
        (0.0, 0.0), (1.0, 0.0), (1.0, 1.0)
    )
    assert _polygon_from_zone(
        {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}
    ) == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))


# --- Timezone -------------------------------------------------------------------------

def test_site_time_converts_and_degrades_safely():
    """Rule schedules are written in local time; evaluating them in UTC shifts every one."""
    local = _to_site_time(NOW, "Asia/Kolkata")
    assert (local.hour, local.minute) == (7, 30)

    # An unknown zone must not raise - it costs correct scheduling, not the alert.
    assert _to_site_time(NOW, "Mars/Olympus").hour == 2
    assert _to_site_time(NOW, None).hour == 2
    # A naive timestamp is read as UTC rather than rejected.
    assert _to_site_time(NOW.replace(tzinfo=None), None).tzinfo is dt.UTC


# --- The chain ------------------------------------------------------------------------

async def test_detection_inside_the_zone_opens_an_incident_and_alerts(ctx):
    await add_rule(ctx)
    result = await run_ingest(ctx)

    assert result.incident_created is True
    assert result.incident_number == 1
    assert result.notifications_scheduled == 1
    assert result.rules_evaluated == 1


async def test_detection_outside_the_zone_records_but_does_not_alert(ctx):
    """The detection is still stored: "what did the camera see at 02:00" is a real question."""
    await add_rule(ctx)
    result = await run_ingest(ctx, objects=(OUTSIDE,))

    assert result.incident_id is None
    assert result.notifications_scheduled == 0
    assert result.detection_id  # recorded regardless
    assert result.rejected_reasons  # and it says why


async def test_replayed_event_does_not_alert_again(ctx):
    """The behaviour that stops one intrusion becoming a stream of 3am calls."""
    await add_rule(ctx)
    first = await run_ingest(ctx, event_suffix="replay")
    second = await run_ingest(ctx, event_suffix="replay")

    assert first.incident_created is True
    assert second.duplicate is True
    assert second.incident_created is False
    assert second.notifications_scheduled == 0


async def test_continuing_incident_does_not_rearm_escalation(ctx):
    """Frame forty of the same person must not re-notify anyone."""
    await add_rule(ctx)
    first = await run_ingest(ctx, event_suffix="f1")
    second = await run_ingest(ctx, event_suffix="f2", now=NOW + dt.timedelta(seconds=2))

    assert first.incident_created is True
    assert second.incident_created is False
    assert second.incident_id == first.incident_id
    assert second.notifications_scheduled == 0


async def test_no_rules_means_no_incident_but_still_a_detection(ctx):
    result = await run_ingest(ctx)

    assert result.rules_evaluated == 0
    assert result.incident_id is None
    assert result.detection_id


async def test_overnight_rule_is_evaluated_in_site_local_time(ctx):
    """02:00 UTC is 07:30 in Kolkata, which is outside a 22:00-06:00 window.

    Read as UTC it would fall *inside* the window and fire - so this test fails if the
    timezone conversion is dropped.
    """
    await add_rule(ctx, from_hour=22, to_hour=6)
    result = await run_ingest(ctx)

    assert result.incident_id is None
    assert "outside_schedule" in " ".join(result.rejected_reasons).lower()


async def test_absurd_object_count_is_truncated_not_fatal(ctx):
    """A malfunctioning edge must not be able to stall ingestion for a whole tenant."""
    await add_rule(ctx)
    flood = [
        DetectedObject("person", 0.5, (0.40, 0.40, 0.45, 0.60))
        for _ in range(MAX_OBJECTS_PER_FRAME + 50)
    ]
    result = await run_ingest(ctx, objects=[INSIDE, *flood])

    assert result.incident_created is True
