"""fan_out_due_outbox_events() and claim_and_send_one_delivery() - the two passes behind
automatic webhook delivery. Real DB (migrations 0001/0045 already provide every table
used here - no new migration). `send_fn` is injected so no real network call happens in
these tests; scripts/e2e_webhook_dispatch.py proves the real HTTP path against
httpbin.org, the same split test_webhooks.py/e2e_webhooks.py already uses.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.security.envelope import KeyRing, generate_master_key
from csense_shared.security.secret_store import write_secret
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE
from csense_shared.webhooks.dispatcher import (
    MAX_DELIVERY_ATTEMPTS,
    WEBHOOK_DISPATCH_CONSUMER,
    claim_and_send_one_delivery,
    fan_out_due_outbox_events,
)

PLATFORM_TEST_EVENT_TYPE = "platform.something.v1"

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _async_dsn(env_var: str = "TEST_POSTGRES_DSN") -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing({"v1": generate_master_key()}, "v1")


@pytest_asyncio.fixture()
async def ctx(keyring):
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Webhook Dispatch Test {suffix}", "s": f"webhook-dispatch-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()

        # A public IP literal, not a hostname: claim_and_send_one_delivery re-runs the
        # SSRF check (resolve_public_endpoint) on every send, and `.test` is an RFC 2606
        # reserved TLD that by definition never resolves - a hostname there turns every
        # send into "Address no longer permitted" before send_fn is ever reached. A
        # numeric host is resolved locally by getaddrinfo, so the real guard still runs
        # and these tests still make no network call of any kind.
        url_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=URL_SECRET_PURPOSE,
            plaintext="https://93.184.216.34/hook", label="test",
        )
        signing_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=SIGNING_SECRET_PURPOSE,
            plaintext="whsec_test_secret", label="test",
        )
        endpoint_id = (
            await session.execute(
                text(
                    "INSERT INTO webhook_endpoints "
                    "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, event_filters, status) "
                    "VALUES (:t, 'Test Endpoint', :url_id, '93.184.216.34', :sig_id, :filters, 'active') RETURNING id"
                ),
                {"t": tenant_id, "url_id": url_secret_id, "sig_id": signing_secret_id, "filters": []},
            )
        ).scalar_one()

        # This consumer is the first thing ever to read `processed_events`, so on a
        # database that has been used before (the dev stack's own, which every e2e script
        # writes outbox rows into) its whole outbox history looks un-fanned-out. Fan-out
        # is `ORDER BY occurred_at LIMIT :limit`, so that backlog is strictly older than
        # anything a test inserts and would fill every batch - each test would measure the
        # backlog rather than its own event. Mark it seen up front, exactly as the real
        # dispatcher's own first pass would: the teardown below deletes every row this
        # consumer owns, so nothing leaks between tests or out of the suite.
        await session.execute(
            text(
                "INSERT INTO processed_events (consumer_name, event_id) "
                "SELECT :c, id FROM outbox_events ON CONFLICT DO NOTHING"
            ),
            {"c": WEBHOOK_DISPATCH_CONSUMER},
        )

    yield {
        "factory": factory, "tenant_id": tenant_id, "org_id": org_id, "endpoint_id": endpoint_id,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM webhook_endpoints WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
        # The tenant-less event the platform-level test inserts has no tenant_id to clean
        # up by, so it would otherwise accumulate in the database one row per run. Its
        # event_type exists only in this file, so deleting by that name is self-scoped.
        await session.execute(
            text("DELETE FROM outbox_events WHERE tenant_id IS NULL AND event_type = :et"),
            {"et": PLATFORM_TEST_EVENT_TYPE},
        )
        await session.execute(
            text("DELETE FROM processed_events WHERE consumer_name = :c"), {"c": WEBHOOK_DISPATCH_CONSUMER}
        )
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})

    await engine.dispose()


async def _insert_outbox_event(ctx, *, event_type: str = "incident.created.v1") -> uuid.UUID:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        event_id = (
            await session.execute(
                text(
                    "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                    "VALUES (:t, 'incident', :agg, :et, CAST(:p AS jsonb)) RETURNING id"
                ),
                {"t": ctx["tenant_id"], "agg": str(uuid.uuid4()), "et": event_type, "p": json.dumps({"x": 1})},
            )
        ).scalar_one()
    return event_id


async def _insert_outbox_event_at(ctx, occurred_at: dt.datetime) -> uuid.UUID:
    """An outbox event with an explicit occurred_at, for the age-cutoff tests."""
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        event_id = (
            await session.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(tenant_id, aggregate_type, aggregate_id, event_type, payload, occurred_at) "
                    "VALUES (:t, 'incident', :agg, 'incident.created.v1', CAST(:p AS jsonb), :at) RETURNING id"
                ),
                {
                    "t": ctx["tenant_id"], "agg": str(uuid.uuid4()),
                    "p": json.dumps({"x": 1}), "at": occurred_at,
                },
            )
        ).scalar_one()
    return event_id


async def _fake_send(status_code: int, elapsed_ms: int = 42):
    # The first argument is a `PinnedEndpoint`, not a URL - the dispatcher resolves and
    # validates the destination itself and hands the sender only the approved addresses.
    async def send_fn(pinned, body, headers):
        return status_code, elapsed_ms
    return send_fn


@pytest.mark.asyncio
async def test_fan_out_creates_one_delivery_per_matching_active_endpoint(ctx):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        fanned = await fan_out_due_outbox_events(session, limit=25)
        rows = (
            await session.execute(
                text("SELECT status, event_type FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).all()
    assert fanned == 1
    assert len(rows) == 1
    assert rows[0][0] == "pending"
    assert rows[0][1] == "incident.created.v1"


@pytest.mark.asyncio
async def test_fan_out_is_idempotent_across_passes(ctx):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        second_pass = await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert second_pass == 0
    assert count == 1


@pytest.mark.asyncio
async def test_fan_out_skips_endpoints_that_do_not_match_event_filters(ctx):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(
            text("UPDATE webhook_endpoints SET event_filters = :f WHERE id = :id"),
            {"f": ["camera.offline.v1"], "id": ctx["endpoint_id"]},
        )
    await _insert_outbox_event(ctx, event_type="incident.created.v1")
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_an_event_older_than_the_age_window_is_never_fanned_out(ctx):
    """The first-deploy case: a database with existing outbox history must not blast every
    past event at a brand-new endpoint. Also covers a worker that was down for days."""
    await _insert_outbox_event_at(ctx, dt.datetime.now(dt.UTC) - dt.timedelta(days=3))
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        fanned = await fan_out_due_outbox_events(session, limit=25, max_event_age_seconds=24 * 60 * 60)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert fanned == 0
    assert count == 0


@pytest.mark.asyncio
async def test_an_event_just_inside_the_age_window_is_still_fanned_out(ctx):
    """The other side of the boundary - proves the cutoff excludes stale events rather
    than simply rejecting everything."""
    await _insert_outbox_event_at(ctx, dt.datetime.now(dt.UTC) - dt.timedelta(hours=23))
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        fanned = await fan_out_due_outbox_events(session, limit=25, max_event_age_seconds=24 * 60 * 60)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert fanned == 1
    assert count == 1


@pytest.mark.asyncio
async def test_claim_and_send_marks_success_on_2xx(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)

    send_fn = await _fake_send(200)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text("SELECT status, response_status, next_attempt_at FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "succeeded"
    assert row[0] == "succeeded"
    assert row[1] == 200
    assert row[2] is None


@pytest.mark.asyncio
async def test_claim_and_send_schedules_a_retry_on_failure_status(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)

    send_fn = await _fake_send(500)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text(
                    "SELECT status, attempt_number, next_attempt_at > now() "
                    "FROM webhook_deliveries WHERE tenant_id = :t"
                ),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "failed"
    assert row[0] == "pending"  # still retryable
    assert row[1] == 2
    assert row[2] is True


@pytest.mark.asyncio
async def test_claim_and_send_abandons_after_max_attempts(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET attempt_number = :n, next_attempt_at = now() WHERE tenant_id = :t"
            ),
            {"n": MAX_DELIVERY_ATTEMPTS, "t": ctx["tenant_id"]},
        )

    send_fn = await _fake_send(500)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text("SELECT status, next_attempt_at FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "abandoned"
    assert row[0] == "abandoned"
    assert row[1] is None


@pytest.mark.asyncio
async def test_claim_and_send_returns_none_when_nothing_is_due(ctx, keyring):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=await _fake_send(200))
    assert status is None


@pytest.mark.asyncio
async def test_disabled_endpoint_abandons_the_delivery_without_sending(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        await session.execute(
            text("UPDATE webhook_endpoints SET status = 'disabled' WHERE id = :id"), {"id": ctx["endpoint_id"]}
        )

    called = False

    async def send_fn(pinned, body, headers):
        nonlocal called
        called = True
        return 200, 1

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
    assert status == "abandoned"
    assert called is False


@pytest.mark.asyncio
async def test_the_sender_is_handed_the_validated_address_not_the_hostname(ctx, keyring):
    """The rebinding half of the SSRF guard, asserted where it is actually load-bearing:
    `send_fn` must receive something that can only reach the address the guard just
    approved. Handing it the URL would let httpx resolve the name a second time, and a
    name that answered publicly for the check can answer privately for the connect - the
    exact window `csense_shared.security.outbound`'s docstring exists to close."""
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)

    seen = {}

    async def send_fn(pinned, body, headers):
        seen["urls"] = pinned.urls
        seen["hostname"] = pinned.hostname
        seen["host_header"] = pinned.headers["Host"]
        seen["sni"] = pinned.extensions["sni_hostname"]
        return 200, 3

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)

    # The fixture's endpoint is a literal, so the validated address and the configured
    # host are the same string - what is being pinned down is that the *addresses* are
    # what travel to the sender, and that the hostname survives for Host and SNI.
    assert seen["urls"] == ("https://93.184.216.34:443/hook",)
    assert seen["hostname"] == "93.184.216.34"
    assert seen["host_header"] == "93.184.216.34"
    assert seen["sni"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_a_platform_level_event_with_no_tenant_is_never_fanned_out(ctx):
    # outbox_events.tenant_id is nullable - a platform-level event belongs to no tenant
    # and so has no tenant's webhook endpoints to deliver to.
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(
            text(
                "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                "VALUES (NULL, 'platform', :agg, :et, '{}'::jsonb)"
            ),
            {"agg": str(uuid.uuid4()), "et": PLATFORM_TEST_EVENT_TYPE},
        )
        fanned = await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert fanned == 0
    assert count == 0
