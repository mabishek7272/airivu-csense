"""notification_worker/app/webhook_dispatch.py - the thin run_once/run_forever loop on
top of csense_shared.webhooks.dispatcher (already covered directly by
test_webhook_dispatcher.py). Loaded by file path, the same convention
test_notification_worker.py already established.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.security.envelope import KeyRing, generate_master_key
from csense_shared.security.secret_store import write_secret
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE
from csense_shared.webhooks.dispatcher import WEBHOOK_DISPATCH_CONSUMER

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

# NOTE (learned the hard way in Task 2 — do not "simplify" either of these):
#
#   The endpoint URL below is a public IP *literal*, not a hostname like
#   `https://example.test/hook`. `claim_and_send_one_delivery` re-runs the real SSRF guard
#   (`resolve_public_endpoint`) immediately before sending, and `.test` is an RFC 2606
#   reserved TLD that by definition never resolves — `getaddrinfo` raises, the guard turns
#   that into `BlockedAddressError`, and the delivery fails before the injected `send_fn`
#   is ever called. A numeric host is resolved locally, so the real guard still runs and
#   still makes no network call. (RFC 5737 documentation ranges like 192.0.2.0/24 do NOT
#   work either — Python's `ipaddress` classifies them private, so the guard blocks them.)
#
#   The fixture also marks every pre-existing outbox row as already seen by this consumer.
#   `processed_events` starts empty in any real database, so without this the whole
#   existing outbox backlog (which fan-out selects oldest-first) fills the batch ahead of
#   the row the test just inserted, and `assert stats["fanned"] == 1` gets a much larger
#   number. The 24h age window added in Task 2 does NOT make this unnecessary — recent
#   backlog sits inside the window too, which only makes the failure intermittent instead
#   of fixing it.


def _load_webhook_dispatch():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "notification_worker" / "app" / "webhook_dispatch.py"
    )
    spec = importlib.util.spec_from_file_location("webhook_dispatch_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
                {"n": f"Webhook Loop Test {suffix}", "s": f"webhook-loop-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        url_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=URL_SECRET_PURPOSE,
            plaintext="https://93.184.216.34/hook", label="test",
        )
        signing_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=SIGNING_SECRET_PURPOSE,
            plaintext="whsec_test_secret", label="test",
        )
        await session.execute(
            text(
                "INSERT INTO webhook_endpoints "
                "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, event_filters, status) "
                "VALUES (:t, 'Test', :url_id, '93.184.216.34', :sig_id, :filters, 'active')"
            ),
            {"t": tenant_id, "url_id": url_secret_id, "sig_id": signing_secret_id, "filters": []},
        )
        # Everything already in the outbox is this consumer's backlog, not this test's
        # event - mark it seen up front (exactly what the real dispatcher's first pass
        # does) so the assertions below measure only the row inserted next.
        await session.execute(
            text(
                "INSERT INTO processed_events (consumer_name, event_id) "
                "SELECT :c, id FROM outbox_events ON CONFLICT DO NOTHING"
            ),
            {"c": WEBHOOK_DISPATCH_CONSUMER},
        )
        await session.execute(
            text(
                "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                "VALUES (:t, 'incident', :agg, 'incident.created.v1', '{}'::jsonb)"
            ),
            {"t": tenant_id, "agg": str(uuid.uuid4())},
        )

    yield {"factory": factory, "tenant_id": tenant_id}

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM webhook_endpoints WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(
            text("DELETE FROM processed_events WHERE consumer_name = :c"), {"c": WEBHOOK_DISPATCH_CONSUMER}
        )
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_once_fans_out_and_sends(ctx, keyring, monkeypatch):
    # run_once uses the real httpx-based sender by default; this test replaces it so no
    # real network call happens, while still proving fan-out -> send connects end to end
    # through the loop wrapper (not just through the dispatcher module directly, which
    # test_webhook_dispatcher.py already covers in isolation).
    module = _load_webhook_dispatch()

    async def fake_http_post(url, body, headers):
        return 200, 7

    monkeypatch.setattr(module, "_http_post", fake_http_post)

    stats = await module.run_once(ctx["factory"], keyring, batch_size=10)
    assert stats["fanned"] == 1
    assert stats["sent"] == 1

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        row = (
            await session.execute(
                text("SELECT status FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).first()
    assert row[0] == "succeeded"


@pytest.mark.asyncio
async def test_run_once_is_a_no_op_pass_when_nothing_is_due(ctx, keyring, monkeypatch):
    module = _load_webhook_dispatch()

    async def fake_http_post(url, body, headers):
        return 200, 1

    monkeypatch.setattr(module, "_http_post", fake_http_post)

    await module.run_once(ctx["factory"], keyring, batch_size=10)  # drains the one seeded event
    stats = await module.run_once(ctx["factory"], keyring, batch_size=10)
    assert stats == {"fanned": 0, "sent": 0}
