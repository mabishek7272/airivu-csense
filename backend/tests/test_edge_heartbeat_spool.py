"""Spool telemetry on the heartbeat, and the degradation verdict drawn from it (FLOW-13).

An offline edge device keeps running its pipeline and writes events into an encrypted
local spool. Two numbers describe that spool: how deep it is right now, and how many
events it has had to evict because it filled. Only the second one is a fault, and the
tests that matter here are the ones pinning *which* of them means what:

  **Depth is not degradation.** A device offline for a day is supposed to have a deep
  spool - that is the spool doing its job. A platform that called it degraded would fire
  an alert every time a site's broadband blinked.

  **Dropped events are degradation, but only while they are still happening.**
  `spool_dropped` is a cumulative counter living with the spool file on the device, so a
  box that evicted one event a fortnight ago reports the same number forever. Marking it
  degraded on `> 0` would latch the status permanently on data that stopped being true
  weeks ago. The delta against the previous heartbeat is what the rule reads, and
  `test_a_stale_drop_count_does_not_hold_the_device_degraded` is the test that proves it
  cannot stick.

The pure-function tests need nothing; the handler tests need a migrated database and are
skipped without one.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from ingest_harness import async_dsn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler

NOW = dt.datetime(2026, 9, 2, 11, 0, tzinfo=dt.UTC)


def _load_edge_module():
    """Loads tenant_api's `app.api.edge` by path.

    Every service under backend/ names its package `app`, so a plain import resolves to
    whichever service another test module happened to import first. Same fix, and the same
    reasoning, as `ingest_harness._load_ingest_module`: save and clear the cached `app*`
    modules, put this service's directory on `sys.path` for the load (edge.py really does
    import `app.deps_agent`), then put everything back.
    """
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_edge_under_test", service_dir / "app" / "api" / "edge.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(service_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


edge = _load_edge_module()


def _snapshot(previous, *, depth, dropped):
    return edge._spool_snapshot(previous, depth=depth, dropped=dropped, now=NOW)


# --- The delta, and why it is the delta -------------------------------------------------

def test_a_first_report_attributes_the_whole_counter_to_now():
    """No prior value means an agent that has never reported spool telemetry before. Its
    counter is history we have not seen, so it is worth exactly one heartbeat of
    `degraded` - said once, loudly - rather than never."""
    block, new_drops = _snapshot(None, depth=4, dropped=3)

    assert new_drops == 3
    assert block["dropped"] == 3
    assert block["depth"] == 4
    assert block["last_dropped_at"] == NOW.isoformat()


def test_an_unchanged_counter_is_no_new_loss():
    block, new_drops = _snapshot({"depth": 0, "dropped": 3}, depth=0, dropped=3)

    assert new_drops == 0
    assert block["dropped"] == 3


def test_a_rising_counter_reports_only_the_difference():
    _, new_drops = _snapshot({"depth": 10, "dropped": 3}, depth=12, dropped=9)

    assert new_drops == 6


def test_a_counter_that_went_backwards_is_treated_as_a_recreated_spool():
    """A reimaged device or a replaced disk restarts the counter from zero. Its current
    value is then loss we have not seen before, not a negative delta to clamp away."""
    _, new_drops = _snapshot({"depth": 900, "dropped": 400}, depth=2, dropped=2)

    assert new_drops == 2


def test_the_last_drop_time_survives_a_clean_heartbeat():
    """The half of the story a self-clearing status would throw away: an operator still
    needs to see that this device *did* lose events, and when, after it has correctly gone
    back to `ok`."""
    earlier = "2026-08-30T03:12:00+00:00"
    block, new_drops = _snapshot(
        {"depth": 0, "dropped": 5, "last_dropped_at": earlier}, depth=0, dropped=5
    )

    assert new_drops == 0
    assert block["last_dropped_at"] == earlier


def test_a_missing_depth_falls_back_to_the_last_one_reported():
    block, _ = _snapshot({"depth": 12, "dropped": 0}, depth=None, dropped=1)

    assert block["depth"] == 12


def test_a_corrupt_previous_snapshot_does_not_break_the_heartbeat():
    """`health` is free-form JSONB. Whatever is in there, a heartbeat must still land -
    a device that cannot report is a device that looks offline."""
    block, new_drops = _snapshot({"dropped": "lots"}, depth=1, dropped=2)

    assert new_drops == 2
    assert block["dropped"] == 2


# --- What the delta is allowed to do to the status --------------------------------------

def test_new_loss_escalates_ok_to_degraded():
    assert edge._health_status_with_spool("ok", 1) == "degraded"


def test_no_new_loss_leaves_the_reported_status_alone():
    assert edge._health_status_with_spool("ok", 0) == "ok"


def test_the_rule_never_downgrades_a_device_that_reports_worse():
    """A device saying `failed` knows something we do not."""
    assert edge._health_status_with_spool("failed", 3) == "failed"
    assert edge._health_status_with_spool("degraded", 3) == "degraded"


# --- Against a real database ------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


@asynccontextmanager
async def _heartbeat_context():
    """One tenant with one enrolled device holding a real agent credential, and the edge
    router on a real ASGI app - so the heartbeat authenticates the way a device actually
    does, through `edge_agent_lookup`, rather than through an overridden dependency."""
    engine = create_async_engine(async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    agent_token = f"hb{suffix}{uuid.uuid4().hex}"

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Heartbeat Spool Test {suffix}", "s": f"hb-spool-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        device_id = (await session.execute(
            text("INSERT INTO edge_devices (tenant_id, name, status, agent_token_prefix, "
                 "agent_token_hash, agent_token_issued_at) "
                 "VALUES (:t,'Spooling gateway','enrolled',:p,:h, now()) RETURNING id"),
            {"t": tenant_id, "p": agent_token[:8],
             "h": hashlib.sha256(agent_token.encode()).hexdigest()},
        )).scalar_one()

    api = FastAPI()
    api.include_router(edge.router)
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = factory

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://edge.test") as client:
            yield {
                "client": client, "factory": factory, "tenant_id": tenant_id,
                "device_id": device_id,
                "auth": {"Authorization": f"Bearer {agent_token}"},
            }
    finally:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            await session.execute(
                text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
            )
            await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await engine.dispose()


@pytest_asyncio.fixture()
async def ctx():
    async with _heartbeat_context() as value:
        yield value


async def _beat(ctx, **body):
    return await ctx["client"].post(
        "/api/v1/tenant/edge/heartbeat", json=body, headers=ctx["auth"]
    )


async def _stored(ctx) -> tuple[dict, str]:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        row = (await session.execute(
            text("SELECT health, health_status FROM edge_devices WHERE id = :id"),
            {"id": ctx["device_id"]},
        )).one()
    return row[0] or {}, row[1]


@pytestmark_db
async def test_both_fields_reach_the_health_snapshot(ctx):
    """The whole point of the field existing: the UI is documented as showing spool use,
    and until now the number had nowhere to arrive."""
    response = await _beat(ctx, status="ok", spool_depth=412, spool_dropped=0)

    assert response.status_code == 200
    health, _ = await _stored(ctx)
    assert health["spool"]["depth"] == 412
    assert health["spool"]["dropped"] == 0


@pytestmark_db
async def test_a_deep_spool_alone_is_not_degraded(ctx):
    """A device offline for a day has thousands of rows waiting and is working exactly as
    designed."""
    await _beat(ctx, status="ok", spool_depth=50_000, spool_dropped=0)

    _, health_status = await _stored(ctx)
    assert health_status == "ok"


@pytestmark_db
async def test_newly_dropped_events_mark_the_device_degraded(ctx):
    """Events evicted from the spool are gone permanently, and the agent whose spool is
    overflowing is the one least likely to say so itself - so the platform overrides its
    cheerful `ok`."""
    await _beat(ctx, status="ok", spool_depth=10_000, spool_dropped=0)
    await _beat(ctx, status="ok", spool_depth=10_000, spool_dropped=17)

    health, health_status = await _stored(ctx)
    assert health_status == "degraded"
    assert health["spool"]["dropped_since_last_heartbeat"] == 17


@pytestmark_db
async def test_a_stale_drop_count_does_not_hold_the_device_degraded(ctx):
    """The anti-latching test, and the reason the rule reads a delta rather than the
    counter. A device that dropped events once and then recovered must not be `degraded`
    for the rest of its service life on the strength of a number that stopped changing."""
    await _beat(ctx, status="ok", spool_depth=900, spool_dropped=0)
    await _beat(ctx, status="ok", spool_depth=1000, spool_dropped=4)
    _, during = await _stored(ctx)

    # Uplink restored: the spool drains, the cumulative counter stays at 4 forever.
    await _beat(ctx, status="ok", spool_depth=0, spool_dropped=4)
    health, after = await _stored(ctx)

    assert during == "degraded"
    assert after == "ok"
    # The fact does not disappear with the status - it just stops being a verdict.
    assert health["spool"]["dropped"] == 4
    assert health["spool"]["last_dropped_at"]


@pytestmark_db
async def test_an_agent_that_reports_no_spool_at_all_is_untouched(ctx):
    """Both fields are optional because a fleet already in the field predates them. Such a
    device must heartbeat exactly as it did before, with no spool key invented for it."""
    response = await _beat(ctx, status="ok", health={"infrastructure": {"tunnel": "up"}})

    assert response.status_code == 200
    health, health_status = await _stored(ctx)
    assert "spool" not in health
    assert health_status == "ok"


@pytestmark_db
async def test_the_agents_own_health_snapshot_is_not_clobbered(ctx):
    """The spool block is added to what the device reported, not substituted for it."""
    await _beat(
        ctx, status="ok", health={"quality": {"fps": 2.0}}, spool_depth=1, spool_dropped=0
    )

    health, _ = await _stored(ctx)
    assert health["quality"] == {"fps": 2.0}
    assert health["spool"]["depth"] == 1


@pytestmark_db
async def test_a_negative_count_is_refused(ctx):
    """A negative depth is a bug in the agent, and storing it would put a nonsense number
    on an operator's screen with nothing to say where it came from."""
    assert (await _beat(ctx, status="ok", spool_depth=-1)).status_code == 422


@pytestmark_db
async def test_an_absurd_count_is_refused(ctx):
    """These numbers come from a device and end up in a JSON document; no real edge disk
    holds two billion spooled events."""
    assert (
        await _beat(ctx, status="ok", spool_depth=edge.MAX_SPOOL_COUNTER + 1)
    ).status_code == 422


@pytestmark_db
async def test_the_degraded_verdict_is_visible_on_the_device_record(ctx):
    """`health_status` was written by every heartbeat and returned by nothing, so this
    verdict would have been invisible to the UI that is meant to act on it."""
    await _beat(ctx, status="ok", spool_depth=5, spool_dropped=0)
    await _beat(ctx, status="ok", spool_depth=5, spool_dropped=2)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        device = await edge._load(session, ctx["device_id"])

    assert device.health_status == "degraded"
    assert device.health["spool"]["depth"] == 5
