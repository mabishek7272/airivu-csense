"""`GET /api/v1/tenant/webhooks/{webhook_id}/deliveries` - real-DB API tests for the
endpoint that closes the gap CHECKLIST.md's webhooks section named plainly: delivery
history (status, attempt count, response code, redacted failure reason) was recorded
correctly by both `test_webhook` and `csense_shared`'s outbox-driven dispatch worker, but
nothing exposed it - only `psql` against a live container did (see
`scripts/e2e_webhook_dispatch.py`'s own docstring).

Signature math is covered elsewhere (`test_webhooks.py`, pure functions); the on-demand
`POST /{id}/test` and endpoint create/list/patch/delete/rotate-secret paths are covered
end-to-end against the real running stack by `scripts/e2e_webhooks.py`. This file proves,
against a live database rather than by inspection, the three things specific to the new
read endpoint: real rows come back correctly shaped and paginated, another tenant's
deliveries never appear, and a missing/foreign `webhook_id` is refused exactly like every
other ownership check `webhooks.py` already makes.

Same two-DSN discipline as `test_pipeline_assignments_api.py`'s own module docstring:
`TEST_POSTGRES_DSN` (`csense_app`, BYPASSRLS) sets up/tears down fixture rows only; the
ASGI app under test is mounted on `TEST_POSTGRES_API_DSN` (`csense_api`), the real role a
production tenant-api container connects as - so RLS is actually enforced by the tests,
not merely trusted.
"""
from __future__ import annotations

import datetime as dt
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

REQUIRED_DSNS = ("TEST_POSTGRES_DSN", "TEST_POSTGRES_API_DSN")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED_DSNS),
    reason=f"integration DSNs not set ({', '.join(REQUIRED_DSNS)}) - skipping",
)


def _load_webhooks_module():
    """Loads tenant_api's `app.api.webhooks` by path - same fix, same reasoning, as
    `test_pipeline_assignments_api.py`'s own loader: every service under backend/ names
    its package `app`, so a plain import would resolve to whichever service another test
    module happened to import first."""
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_webhooks_under_test",
            service_dir / "app" / "api" / "webhooks.py",
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


webhooks = _load_webhooks_module()
_get_app_settings = webhooks.current_tenant_context.__globals__["get_app_settings"]


def _async_dsn(env_var: str) -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _make_tenant(session, *, suffix: str) -> uuid.UUID:
    org_id = (
        await session.execute(
            text(
                "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
            ),
            {"n": f"Webhook Deliveries API Test {suffix}", "s": f"webhook-deliveries-api-test-{suffix}"},
        )
    ).scalar_one()
    tenant_id = (
        await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
            {"o": org_id},
        )
    ).scalar_one()
    return tenant_id


async def _make_dummy_secret(session, *, tenant_id: uuid.UUID, purpose: str) -> uuid.UUID:
    """A placeholder `encrypted_secrets` row - `webhook_endpoints.url_secret_id`/
    `signing_secret_id` carry a real FK to this table (migration 0045), so a bare random
    UUID would violate it. Nothing in these tests ever decrypts these values through
    `read_secret`, so plain placeholder text satisfying the column's own check
    constraints (`ck_secret_purpose`, `ck_secret_kek_id`) is enough - going through the
    real envelope/keyring machinery just to satisfy a foreign key would test nothing
    extra here."""
    return (
        await session.execute(
            text(
                "INSERT INTO encrypted_secrets "
                "(tenant_id, purpose, kek_id, wrapped_dek, dek_nonce, ciphertext, ciphertext_nonce) "
                "VALUES (:tenant_id, :purpose, 'test-kek', 'x', 'x', 'x', 'x') RETURNING id"
            ),
            {"tenant_id": tenant_id, "purpose": purpose},
        )
    ).scalar_one()


async def _make_endpoint(session, *, tenant_id: uuid.UUID, suffix: str) -> uuid.UUID:
    """A webhook endpoint row good enough for delivery-history tests."""
    url_secret_id = await _make_dummy_secret(session, tenant_id=tenant_id, purpose="webhook.url")
    signing_secret_id = await _make_dummy_secret(session, tenant_id=tenant_id, purpose="webhook.signing")
    return (
        await session.execute(
            text(
                "INSERT INTO webhook_endpoints "
                "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, created_by) "
                "VALUES (:tenant_id, :name, :url_secret_id, :host, :signing_secret_id, :created_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id, "name": f"API Test Endpoint {suffix}",
                "url_secret_id": url_secret_id, "host": "example.test",
                "signing_secret_id": signing_secret_id, "created_by": uuid.uuid4(),
            },
        )
    ).scalar_one()


async def _make_delivery(
    session, *, tenant_id: uuid.UUID, endpoint_id: uuid.UUID, event_type: str, status: str,
    scheduled_at: dt.datetime, response_status: int | None = None, failure: str | None = None,
) -> uuid.UUID:
    sent_at = scheduled_at if status == "succeeded" else None
    return (
        await session.execute(
            text(
                "INSERT INTO webhook_deliveries "
                "(tenant_id, webhook_endpoint_id, event_type, payload, status, scheduled_at, "
                " sent_at, response_status, response_time_ms, failure_summary_redacted) "
                "VALUES (:tenant_id, :endpoint_id, :event_type, '{}'::jsonb, "
                " CAST(:status AS webhook_delivery_status), :scheduled_at, :sent_at, "
                " :response_status, 120, :failure) "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id, "endpoint_id": endpoint_id, "event_type": event_type,
                "status": status, "scheduled_at": scheduled_at, "sent_at": sent_at,
                "response_status": response_status, "failure": failure,
            },
        )
    ).scalar_one()


@asynccontextmanager
async def _api_context(settings):
    owner_engine = create_async_engine(_async_dsn("TEST_POSTGRES_DSN"))
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    api_engine = create_async_engine(_async_dsn("TEST_POSTGRES_API_DSN"))
    api_factory = async_sessionmaker(api_engine, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:8]
    base_time = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    tenant_ids: list[uuid.UUID] = []

    async with owner_factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))

        tenant_id = await _make_tenant(session, suffix=f"main-{suffix}")
        other_tenant_id = await _make_tenant(session, suffix=f"other-{suffix}")
        tenant_ids = [tenant_id, other_tenant_id]

        endpoint_id = await _make_endpoint(session, tenant_id=tenant_id, suffix=f"main-{suffix}")
        other_tenants_endpoint_id = await _make_endpoint(session, tenant_id=other_tenant_id, suffix=f"other-{suffix}")

        # Three deliveries on the endpoint under test, spread across statuses and time,
        # oldest to newest so descending order is actually exercised rather than
        # coincidentally matching insertion order.
        succeeded_id = await _make_delivery(
            session, tenant_id=tenant_id, endpoint_id=endpoint_id, event_type="incident.acknowledged.v1",
            status="succeeded", scheduled_at=base_time, response_status=200,
        )
        failed_id = await _make_delivery(
            session, tenant_id=tenant_id, endpoint_id=endpoint_id, event_type="incident.dismissed.v1",
            status="failed", scheduled_at=base_time + dt.timedelta(minutes=5), response_status=500,
            failure="destination returned 500 [redacted]",
        )
        pending_id = await _make_delivery(
            session, tenant_id=tenant_id, endpoint_id=endpoint_id, event_type="incident.resolved.v1",
            status="pending", scheduled_at=base_time + dt.timedelta(minutes=10),
        )

        # A delivery that belongs to another tenant's endpoint entirely - must never
        # appear no matter what is queried for the endpoint under test.
        await _make_delivery(
            session, tenant_id=other_tenant_id, endpoint_id=other_tenants_endpoint_id,
            event_type="incident.acknowledged.v1", status="succeeded",
            scheduled_at=base_time, response_status=200,
        )

    api = FastAPI()
    api.include_router(webhooks.router)
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = api_factory
    api.dependency_overrides[_get_app_settings] = lambda: settings

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://tenant.test") as client:
            yield {
                "client": client, "tenant_id": tenant_id, "other_tenant_id": other_tenant_id,
                "endpoint_id": endpoint_id, "other_tenants_endpoint_id": other_tenants_endpoint_id,
                "succeeded_id": succeeded_id, "failed_id": failed_id, "pending_id": pending_id,
            }
    finally:
        async with owner_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            for tid in tenant_ids:
                await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tid})
            await session.execute(
                text("DELETE FROM organizations WHERE display_name LIKE :pattern"),
                {"pattern": f"Webhook Deliveries API Test %{suffix}"},
            )
        await owner_engine.dispose()
        await api_engine.dispose()


@pytest_asyncio.fixture()
async def ctx(settings):
    async with _api_context(settings) as value:
        yield value


def _headers(settings, *, tenant_id, permissions=("webhook.manage",)) -> dict:
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=uuid.uuid4(),
        permissions=frozenset(permissions), session_id=uuid.uuid4().hex,
    )
    return {"Authorization": f"Bearer {token}"}


async def _list_deliveries(ctx, headers, *, webhook_id=None, **params):
    if webhook_id is None:
        webhook_id = ctx["endpoint_id"]
    return await ctx["client"].get(
        f"/api/v1/tenant/webhooks/{webhook_id}/deliveries", headers=headers, params=params,
    )


# --- Shape and content -------------------------------------------------------------------

async def test_real_delivery_rows_come_back_correctly_shaped(ctx, settings):
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]))

    assert response.status_code == 200, response.text
    body = response.json()
    ids = {row["id"] for row in body["items"]}
    assert ids == {str(ctx["succeeded_id"]), str(ctx["failed_id"]), str(ctx["pending_id"])}

    failed = next(r for r in body["items"] if r["event_type"] == "incident.dismissed.v1")
    assert failed["status"] == "failed"
    assert failed["response_status"] == 500
    assert failed["failure_summary_redacted"] == "destination returned 500 [redacted]"
    assert failed["attempt_number"] == 1
    assert failed["response_time_ms"] == 120

    succeeded = next(r for r in body["items"] if r["event_type"] == "incident.acknowledged.v1")
    assert succeeded["status"] == "succeeded"
    assert succeeded["sent_at"] is not None

    pending = next(r for r in body["items"] if r["event_type"] == "incident.resolved.v1")
    assert pending["status"] == "pending"
    assert pending["sent_at"] is None
    assert pending["response_status"] is None
    assert pending["failure_summary_redacted"] is None


async def test_ordering_is_newest_first(ctx, settings):
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]))
    event_types = [row["event_type"] for row in response.json()["items"]]
    assert event_types == ["incident.resolved.v1", "incident.dismissed.v1", "incident.acknowledged.v1"]


async def test_status_filter_narrows_results(ctx, settings):
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]), status="failed")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


async def test_a_comma_separated_status_filter_matches_the_incidents_list_convention(ctx, settings):
    response = await _list_deliveries(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"]), status="failed,pending",
    )
    assert response.status_code == 200, response.text
    statuses = {row["status"] for row in response.json()["items"]}
    assert statuses == {"failed", "pending"}


async def test_an_unknown_status_value_is_refused_with_a_clear_code(ctx, settings):
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]), status="bogus")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_status"


async def test_pagination_returns_one_page_at_a_time_with_no_gaps_or_repeats(ctx, settings):
    headers = _headers(settings, tenant_id=ctx["tenant_id"])

    first = await _list_deliveries(ctx, headers, limit=2)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second = await _list_deliveries(ctx, headers, limit=2, cursor=first_body["next_cursor"])
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["next_cursor"] is None

    seen = [row["id"] for row in first_body["items"]] + [row["id"] for row in second_body["items"]]
    assert len(seen) == len(set(seen)), "no delivery repeated across pages"


async def test_a_malformed_cursor_is_refused_not_500ed(ctx, settings):
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]), cursor="not-a-real-cursor")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


# --- Tenant isolation ---------------------------------------------------------------------

async def test_another_tenants_deliveries_never_appear(ctx, settings):
    """Even though `webhook_deliveries` is queried without an explicit tenant filter in
    the endpoint itself, RLS (migration 0045) must still keep the other tenant's row out
    - this proves the database policy holds against the real `csense_api` role, not just
    that the SQL happens to filter by `webhook_endpoint_id`."""
    response = await _list_deliveries(ctx, _headers(settings, tenant_id=ctx["tenant_id"]))
    ids = {row["id"] for row in response.json()["items"]}
    assert len(ids) == 3, "only the three deliveries belonging to this tenant's endpoint"


async def test_a_foreign_tenants_webhook_id_is_refused_like_a_missing_one(ctx, settings):
    """Same 'missing vs. not yours stays indistinguishable' discipline `update_webhook`/
    `delete_webhook`/`rotate_webhook_secret`/`test_webhook` already apply - a real
    endpoint id belonging to a different tenant must get exactly the 404 a nonexistent id
    would, never a 403 or any other signal that it exists."""
    cross_tenant = await _list_deliveries(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"]), webhook_id=ctx["other_tenants_endpoint_id"],
    )
    missing = await _list_deliveries(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"]), webhook_id=str(uuid.uuid4()),
    )
    assert cross_tenant.status_code == 404
    assert missing.status_code == 404
    assert cross_tenant.json()["code"] == missing.json()["code"]


# --- Permission gate, proven against the real route ---------------------------------------

async def test_an_unauthenticated_call_is_rejected(ctx):
    response = await _list_deliveries(ctx, headers={})
    assert response.status_code == 401


async def test_a_token_without_webhook_manage_is_rejected(ctx, settings):
    response = await _list_deliveries(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"], permissions=("camera.read",)),
    )
    assert response.status_code == 403
