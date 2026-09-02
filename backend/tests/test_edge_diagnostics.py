"""Task 2 of docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-
state.md: `GET /devices/{id}/diagnostics`.

A real user-facing route for once in this file's neighbourhood (`test_edge_heartbeat_
spool.py` and `test_edge_sync.py` both authenticate as a *device*, via its agent
credential). This one is gated by `require_permission(context, "diagnostic.read")` and
reached through `current_tenant_context`, so it needs a real signed access token the way
an ordinary tenant user's browser session would carry one - built with `issue_access_
token` against the same throwaway RSA keypair `conftest.py`'s `settings` fixture already
generates per test, with `app/deps.py`'s own `get_app_settings` dependency overridden to
serve that same `settings` object rather than the real deployment's `/run/secrets/*.pem`
(which this host process cannot read, and must not need to).

RLS is the property most worth proving for real, not by inspection - `test_tenant_
isolation.py`'s own convention (a real second tenant/device, never a fabricated id) is
followed here for exactly the same reason: a route that *looks* tenant-scoped because it
calls `_load()` is not the same claim as one proven not to leak across a second real
tenant.
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

# Two distinct roles, deliberately - the same distinction test_tenant_isolation.py's own
# docstring draws: TEST_POSTGRES_DSN (`csense_app`) is the database owner, a superuser
# with BYPASSRLS - real, but only for setting up and tearing down fixture rows, never for
# asserting isolation, because it would pass a cross-tenant read by accident regardless of
# any policy. TEST_POSTGRES_API_DSN (`csense_api`) is the actual role the real tenant-api
# container connects as in every real deployment (`infra/docker-compose*.yml`'s
# `POSTGRES_USER: ${POSTGRES_API_USER:-csense_api}`, not the default in `Settings`, which
# names the owner role and exists for tooling that runs outside a container). The ASGI
# app this file mounts uses the API role for its `session_factory` for exactly that
# reason: it is the only connection this test can use and still have "tenant B cannot
# read tenant A's device" mean anything.
REQUIRED_DSNS = ("TEST_POSTGRES_DSN", "TEST_POSTGRES_API_DSN")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED_DSNS),
    reason=f"integration DSNs not set ({', '.join(REQUIRED_DSNS)}) - skipping",
)


def _load_edge_module():
    """Loads tenant_api's `app.api.edge` by path - same fix, same reasoning, as
    `test_edge_heartbeat_spool.py`'s own loader: every service under backend/ names its
    package `app`, so a plain import would resolve to whichever service another test
    module happened to import first."""
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_edge_diagnostics_under_test", service_dir / "app" / "api" / "edge.py"
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

# The exact function object `current_tenant_context` resolves its own `Settings` from via
# `Depends(get_app_settings)`. `_load_edge_module` deletes `app.deps` from `sys.modules`
# once loading finishes (see its docstring), so it can't be re-imported by name after the
# fact - but the module object it came from is still alive as long as something holds a
# reference to one of its names, and `current_tenant_context.__globals__` *is* that
# module's own namespace dict. Grabbing `get_app_settings` off it is what lets
# `dependency_overrides` target the identical callable FastAPI would otherwise resolve.
_get_app_settings = edge.current_tenant_context.__globals__["get_app_settings"]


def _async_dsn(env_var: str) -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _make_tenant_device(session, label: str) -> dict:
    suffix = uuid.uuid4().hex[:8]
    agent_token = f"dg{suffix}{uuid.uuid4().hex}"
    org_id = (
        await session.execute(
            text(
                "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
            ),
            {"n": f"Diagnostics Test {label} {suffix}", "s": f"diag-{label}-{suffix}"},
        )
    ).scalar_one()
    tenant_id = (
        await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
            {"o": org_id},
        )
    ).scalar_one()
    device_id = (
        await session.execute(
            text(
                "INSERT INTO edge_devices (tenant_id, name, status, agent_token_prefix, "
                "agent_token_hash, agent_token_issued_at) "
                "VALUES (:t, :n, 'enrolled', :p, :h, now()) RETURNING id"
            ),
            {
                "t": tenant_id, "n": f"Diagnostics device {label}", "p": agent_token[:8],
                "h": hashlib.sha256(agent_token.encode()).hexdigest(),
            },
        )
    ).scalar_one()
    return {"org_id": org_id, "tenant_id": tenant_id, "device_id": device_id, "agent_token": agent_token}


@asynccontextmanager
async def _diagnostics_context(settings):
    """Two real tenants, each with one real enrolled device - so both the RLS-isolation
    tests and the "an ordinary tenant_member reads their own device" test exercise the
    genuine article, not a fabricated id standing in for "someone else's tenant".

    Two engines, two roles, on purpose (see the module docstring's `REQUIRED_DSNS`
    comment): `owner_engine` is the database-owner role, used only to create/tear down
    fixture rows across both tenants and to insert command history directly; the ASGI
    app's own `session_factory` - what `db_session_for_tenant`/`agent_db_session` actually
    read requests through - is built on `api_engine`, the real `csense_api` role every
    production tenant-api container connects as. Mounting the app on the owner role would
    make every RLS assertion below pass by accident, the exact vacuousness
    `test_tenant_isolation.py::test_application_roles_cannot_bypass_rls` guards against.
    """
    owner_engine = create_async_engine(_async_dsn("TEST_POSTGRES_DSN"))
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    api_engine = create_async_engine(_async_dsn("TEST_POSTGRES_API_DSN"))
    api_factory = async_sessionmaker(api_engine, expire_on_commit=False)

    async with owner_factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        tenant_a = await _make_tenant_device(session, "a")
        tenant_b = await _make_tenant_device(session, "b")

    api = FastAPI()
    api.include_router(edge.router)
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = api_factory
    api.dependency_overrides[_get_app_settings] = lambda: settings

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://edge.test") as client:
            yield {
                "client": client, "owner_factory": owner_factory,
                "tenant_a": tenant_a, "tenant_b": tenant_b,
            }
    finally:
        async with owner_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            for tenant in (tenant_a, tenant_b):
                await session.execute(
                    text("DELETE FROM tenants WHERE organization_id = :o"), {"o": tenant["org_id"]}
                )
                await session.execute(
                    text("DELETE FROM organizations WHERE id = :o"), {"o": tenant["org_id"]}
                )
        await owner_engine.dispose()
        await api_engine.dispose()


@pytest_asyncio.fixture()
async def ctx(settings):
    async with _diagnostics_context(settings) as value:
        yield value


def _token(settings, *, tenant_id, permissions):
    return issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=uuid.uuid4(),
        permissions=frozenset(permissions), session_id=uuid.uuid4().hex,
    )


def _auth(settings, *, tenant_id, permissions) -> dict:
    return {"Authorization": f"Bearer {_token(settings, tenant_id=tenant_id, permissions=permissions)}"}


async def _heartbeat(ctx, tenant, **body):
    return await ctx["client"].post(
        "/api/v1/tenant/edge/heartbeat", json=body,
        headers={"Authorization": f"Bearer {tenant['agent_token']}"},
    )


async def _insert_command(
    ctx, tenant, *, command_type, status, result_code, result_summary, issued_at
):
    """Inserted directly rather than through `POST /devices/{id}/commands`: that route
    signs the envelope with the *real* deployment's JWT keypair via `get_settings()`
    called directly (not an injected, overridable dependency) - reasonable in production,
    but it means this test process (no `/run/secrets/*.pem` on the host) cannot exercise
    it. `signed_envelope` is opaque to the diagnostics route - it is returned, never
    decoded - so a placeholder string is a faithful stand-in for what a real issuance
    would have produced."""
    async with ctx["owner_factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text(
                "INSERT INTO device_commands (id, tenant_id, edge_device_id, command_type, "
                "payload, idempotency_key, status, not_before, expires_at, issued_at, "
                "delivered_at, completed_at, result_code, result_summary, signed_envelope) "
                "VALUES (:id, :tenant_id, :device_id, :command_type, '{}'::jsonb, :key, "
                ":status, :issued_at, :expires_at, :issued_at, :issued_at, :issued_at, "
                ":result_code, :result_summary, 'test-envelope')"
            ),
            {
                "id": uuid.uuid4(), "tenant_id": tenant["tenant_id"], "device_id": tenant["device_id"],
                "command_type": command_type, "key": uuid.uuid4().hex, "status": status,
                "issued_at": issued_at, "expires_at": issued_at + dt.timedelta(hours=1),
                "result_code": result_code, "result_summary": result_summary,
            },
        )


async def _diagnostics(ctx, device_id, headers):
    return await ctx["client"].get(
        f"/api/v1/tenant/edge/devices/{device_id}/diagnostics", headers=headers
    )


# --- An ordinary tenant_member reads their own device's diagnostics ---------------------

async def test_own_device_diagnostics_contains_real_log_and_command_content(ctx, settings):
    """The assertion that matters most: not just 200, but that `logs`, health fields, and
    command history genuinely reflect what was written - not a default/empty shape that
    would also satisfy a shallower test."""
    tenant = ctx["tenant_a"]

    # First heartbeat: the buffer as it would look early in a device's uptime.
    await _heartbeat(
        ctx, tenant, status="ok",
        health={
            "logs": [
                {"timestamp": "2026-09-02T03:00:00+00:00", "level": "INFO",
                 "message": "agent_started"},
            ],
        },
        connectivity_method="wireguard", connectivity_reason="tunnel established",
    )
    # A second heartbeat, with a distinctive marker appended - proves the diagnostics
    # route reads the *current* row, not a value cached from the first heartbeat.
    response = await _heartbeat(
        ctx, tenant, status="degraded",
        health={
            "logs": [
                {"timestamp": "2026-09-02T03:00:00+00:00", "level": "INFO",
                 "message": "agent_started"},
                {"timestamp": "2026-09-02T03:00:30+00:00", "level": "WARNING",
                 "message": "heartbeat_failed | detail=connection reset by peer [redacted]"},
            ],
        },
        connectivity_method="wireguard", connectivity_reason="tunnel established",
    )
    assert response.status_code == 200, response.text

    t1 = dt.datetime(2026, 9, 2, 3, 0, tzinfo=dt.UTC)
    t2 = dt.datetime(2026, 9, 2, 3, 1, tzinfo=dt.UTC)
    await _insert_command(
        ctx, tenant, command_type="ping", status="completed",
        result_code="ok", result_summary="pong", issued_at=t1,
    )
    await _insert_command(
        ctx, tenant, command_type="config_push", status="failed",
        result_code="out_of_bounds", result_summary="heartbeat_interval_seconds must be > 0",
        issued_at=t2,
    )

    headers = _auth(settings, tenant_id=tenant["tenant_id"], permissions={"diagnostic.read"})
    got = await _diagnostics(ctx, tenant["device_id"], headers)

    assert got.status_code == 200, got.text
    body = got.json()

    assert body["device_id"] == str(tenant["device_id"])
    assert body["health_status"] == "degraded"
    assert body["connectivity_method"] == "wireguard"
    assert body["connectivity_reason"] == "tunnel established"
    assert body["last_seen_at"] is not None

    # The log tail is the second (latest) heartbeat's content, not the first's and not
    # empty/default.
    messages = [entry["message"] for entry in body["health"]["logs"]]
    assert messages == [
        "agent_started",
        "heartbeat_failed | detail=connection reset by peer [redacted]",
    ]

    # Command history: most recent first, real result codes/summaries, not a placeholder.
    assert [c["command_type"] for c in body["commands"]] == ["config_push", "ping"]
    assert body["commands"][0]["status"] == "failed"
    assert body["commands"][0]["result_code"] == "out_of_bounds"
    assert body["commands"][0]["result_summary"] == "heartbeat_interval_seconds must be > 0"
    assert body["commands"][1]["status"] == "completed"
    assert body["commands"][1]["result_code"] == "ok"
    assert body["commands"][1]["result_summary"] == "pong"
    for command in body["commands"]:
        assert command["issued_at"] is not None
        assert command["delivered_at"] is not None
        assert command["completed_at"] is not None
        # `diagnostic.read` is deliberately a narrower surface than `edge.read`
        # (`GET /devices/{id}/commands`, which returns the full `CommandOut` shape): a
        # support grant scoped to only `["diagnostic.read"]` must not incidentally hand
        # over a command's operational payload or its raw signed envelope - the absence
        # is the point, not merely what's present.
        assert "payload" not in command
        assert "signed_envelope" not in command


# --- A device that has never heartbeated ------------------------------------------------

async def test_a_device_that_has_never_heartbeated_returns_empty_not_an_error(ctx, settings):
    """Every field this route surfaces is written by a heartbeat that, for a freshly
    enrolled device, may never have happened yet. `_make_tenant_device` leaves tenant_b's
    device exactly in that state - no heartbeat sent anywhere in this test - so this
    exercises the real column defaults (`health = {}`, everything else `NULL`) through
    the real route, not just by inspection of `_to_device`."""
    tenant = ctx["tenant_b"]
    headers = _auth(settings, tenant_id=tenant["tenant_id"], permissions={"diagnostic.read"})

    got = await _diagnostics(ctx, tenant["device_id"], headers)

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["health"] == {}
    assert body["health_status"] is None
    assert body["connectivity_method"] is None
    assert body["connectivity_reason"] is None
    assert body["last_seen_at"] is None
    assert body["commands"] == []


# --- RLS: a real second tenant, not a fabricated id --------------------------------------

async def test_a_device_in_a_different_tenant_is_refused(ctx, settings):
    """Tenant B, holding `diagnostic.read` for its own tenant, reads tenant A's real
    device id. RLS makes tenant A's row invisible to a session scoped to tenant B, so this
    must 404 exactly like an unknown device - not 403, which would confirm the device
    exists."""
    tenant_a, tenant_b = ctx["tenant_a"], ctx["tenant_b"]
    await _heartbeat(ctx, tenant_a, status="ok", health={"logs": []})

    headers = _auth(settings, tenant_id=tenant_b["tenant_id"], permissions={"diagnostic.read"})
    got = await _diagnostics(ctx, tenant_a["device_id"], headers)

    assert got.status_code == 404
    assert got.json()["code"] == "not_found"
    assert got.json()["message"] == "No such edge device."


async def test_unknown_device_id_returns_404_identically_to_another_tenants_device(ctx, settings):
    """Same status, code, and message as the cross-tenant case above - the guarantee
    `_load()` already makes everywhere else in this file: "not found" and "not yours"
    must be indistinguishable, or the API becomes a way to enumerate device ids."""
    tenant = ctx["tenant_a"]
    headers = _auth(settings, tenant_id=tenant["tenant_id"], permissions={"diagnostic.read"})

    got = await _diagnostics(ctx, uuid.uuid4(), headers)

    assert got.status_code == 404
    assert got.json()["code"] == "not_found"
    assert got.json()["message"] == "No such edge device."


# --- Permission gate -----------------------------------------------------------------------

async def test_a_request_without_diagnostic_read_is_refused(ctx, settings):
    tenant = ctx["tenant_a"]
    await _heartbeat(ctx, tenant, status="ok", health={"logs": []})

    # A real, but unrelated, permission - proving this isn't "any authenticated token
    # passes", only a token actually carrying diagnostic.read.
    headers = _auth(settings, tenant_id=tenant["tenant_id"], permissions={"edge.read"})
    got = await _diagnostics(ctx, tenant["device_id"], headers)

    assert got.status_code == 403
    assert got.json()["code"] == "not_authorized"
