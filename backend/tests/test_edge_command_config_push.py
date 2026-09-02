"""Task 4 of docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-
state.md, server side: `POST /devices/{id}/config` and `observed_state_version` on
`POST /heartbeat` - `edge_devices.desired_state_version`/`observed_state_version`'s first
real writers (migration 0042 added the columns; nothing wrote to either until this task).

The agent's own response to a config_push command - validating it against `config.py`'s
startup bounds, applying it to the running `Spool`/`SyncEngine`/heartbeat cadence, and
what happens when one is expired - is covered end to end in `test_edge_config_push.py`
(no server, no database - the agent-side unit is `main.py::_run_command`). This file is
the other half: does issuing a config push through the real route actually bump the
version, sign a real envelope, and leave the convergence gap FLOW-13 requires visible on
`GET /devices/{id}` - all against a live database, the same discipline `test_edge_
heartbeat_spool.py` and `test_edge_diagnostics.py` already use for this file's neighbours.
"""
from __future__ import annotations

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

# Same reasoning, same two DSNs, as `test_edge_diagnostics.py`'s own module docstring:
# TEST_POSTGRES_DSN (`csense_app`, BYPASSRLS) sets up/tears down fixture rows only; the
# ASGI app under test is mounted on TEST_POSTGRES_API_DSN (`csense_api`), the real role
# every production tenant-api container connects as - the one connection this test can use
# and still have "the version bump is really scoped to this tenant's own device" mean
# anything.
REQUIRED_DSNS = ("TEST_POSTGRES_DSN", "TEST_POSTGRES_API_DSN")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED_DSNS),
    reason=f"integration DSNs not set ({', '.join(REQUIRED_DSNS)}) - skipping",
)


def _load_edge_module():
    """Loads tenant_api's `app.api.edge` by path - see `test_edge_diagnostics.py`'s own
    loader for why (every service under backend/ names its package `app`)."""
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_edge_config_push_under_test", service_dir / "app" / "api" / "edge.py"
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
# Same trick `test_edge_diagnostics.py` uses to reach the *exact* callable `current_
# tenant_context` resolves its `Settings` through via `Depends(get_app_settings)`.
_get_app_settings = edge.current_tenant_context.__globals__["get_app_settings"]


def _async_dsn(env_var: str) -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _make_tenant_device(session) -> dict:
    suffix = uuid.uuid4().hex[:8]
    agent_token = f"cp{suffix}{uuid.uuid4().hex}"
    org_id = (
        await session.execute(
            text(
                "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
            ),
            {"n": f"Config Push Test {suffix}", "s": f"config-push-{suffix}"},
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
                "t": tenant_id, "n": "Config push device", "p": agent_token[:8],
                "h": hashlib.sha256(agent_token.encode()).hexdigest(),
            },
        )
    ).scalar_one()
    return {"org_id": org_id, "tenant_id": tenant_id, "device_id": device_id, "agent_token": agent_token}


@asynccontextmanager
async def _config_push_context(settings):
    owner_engine = create_async_engine(_async_dsn("TEST_POSTGRES_DSN"))
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    api_engine = create_async_engine(_async_dsn("TEST_POSTGRES_API_DSN"))
    api_factory = async_sessionmaker(api_engine, expire_on_commit=False)

    async with owner_factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        device = await _make_tenant_device(session)

    api = FastAPI()
    api.include_router(edge.router)
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = api_factory
    api.dependency_overrides[_get_app_settings] = lambda: settings
    # `push_config`/`issue_command` call `get_settings()` directly (not an injected
    # dependency - same as `provision_vpn`'s own `get_settings()` call), so signing a real
    # envelope in-process needs the module-level name itself monkeypatched to the same
    # throwaway keypair `settings` already carries, the same way `signed_commands.sign_
    # command`/`verify_signed_command` are exercised in `test_signed_commands.py`.
    original_get_settings = edge.get_settings
    edge.get_settings = lambda: settings

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://edge.test") as client:
            yield {
                "client": client, "owner_factory": owner_factory, "device": device,
            }
    finally:
        edge.get_settings = original_get_settings
        async with owner_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            await session.execute(
                text("DELETE FROM tenants WHERE organization_id = :o"), {"o": device["org_id"]}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": device["org_id"]}
            )
        await owner_engine.dispose()
        await api_engine.dispose()


@pytest_asyncio.fixture()
async def ctx(settings):
    async with _config_push_context(settings) as value:
        yield value


def _owner_headers(settings, *, tenant_id) -> dict:
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=uuid.uuid4(),
        permissions=frozenset({"edge.manage", "edge.read"}), session_id=uuid.uuid4().hex,
    )
    return {"Authorization": f"Bearer {token}"}


def _read_only_headers(settings, *, tenant_id) -> dict:
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=uuid.uuid4(),
        permissions=frozenset({"edge.read"}), session_id=uuid.uuid4().hex,
    )
    return {"Authorization": f"Bearer {token}"}


async def _push_config(ctx, settings, *, idempotency_key=None, **fields):
    device = ctx["device"]
    body = {"idempotency_key": idempotency_key or uuid.uuid4().hex, **fields}
    return await ctx["client"].post(
        f"/api/v1/tenant/edge/devices/{device['device_id']}/config",
        json=body, headers=_owner_headers(settings, tenant_id=device["tenant_id"]),
    )


async def _get_device(ctx, settings):
    device = ctx["device"]
    return await ctx["client"].get(
        f"/api/v1/tenant/edge/devices/{device['device_id']}",
        headers=_owner_headers(settings, tenant_id=device["tenant_id"]),
    )


async def _heartbeat(ctx, **body):
    device = ctx["device"]
    return await ctx["client"].post(
        "/api/v1/tenant/edge/heartbeat", json=body,
        headers={"Authorization": f"Bearer {device['agent_token']}"},
    )


async def _pending_commands(ctx):
    device = ctx["device"]
    return await ctx["client"].get(
        "/api/v1/tenant/edge/commands/pending",
        headers={"Authorization": f"Bearer {device['agent_token']}"},
    )


# --- GET /devices/{id} exposes both counters, defaulting to 0 (Task 2's own claim,
# reverified here specifically for this task's two fields) -------------------------------

async def test_a_freshly_enrolled_device_starts_at_version_zero_both_sides(ctx, settings):
    got = await _get_device(ctx, settings)

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["desired_state_version"] == 0
    assert body["observed_state_version"] == 0


# --- Issuing a config push: the first real writer of desired_state_version --------------

async def test_a_config_push_bumps_desired_state_version_and_signs_a_real_envelope(ctx, settings):
    response = await _push_config(ctx, settings, heartbeat_interval_seconds=5)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["device"]["desired_state_version"] == 1
    assert body["command"]["command_type"] == "config_push"
    assert body["command"]["payload"] == {"heartbeat_interval_seconds": 5, "target_version": 1}
    # A real signed JWT, not a placeholder - decodable with the same throwaway keypair.
    import jwt as pyjwt
    claims = pyjwt.decode(
        body["command"]["signed_envelope"], settings.jwt_public_key,
        algorithms=[settings.jwt_algorithm], audience="csense-edge-command",
        issuer=settings.jwt_issuer,
    )
    assert claims["payload"] == {"heartbeat_interval_seconds": 5, "target_version": 1}


async def test_a_second_config_push_bumps_the_version_again(ctx, settings):
    await _push_config(ctx, settings, heartbeat_interval_seconds=5)
    second = await _push_config(ctx, settings, spool_max_rows=2000)

    assert second.json()["device"]["desired_state_version"] == 2
    assert second.json()["command"]["payload"]["target_version"] == 2


async def test_reissuing_the_same_idempotency_key_does_not_bump_the_version_twice(ctx, settings):
    key = "install-config-v1"
    first = await _push_config(ctx, settings, idempotency_key=key, heartbeat_interval_seconds=5)
    second = await _push_config(ctx, settings, idempotency_key=key, heartbeat_interval_seconds=5)

    assert first.json()["command"]["id"] == second.json()["command"]["id"]
    assert second.json()["device"]["desired_state_version"] == 1


async def test_an_empty_config_push_is_refused(ctx, settings):
    response = await _push_config(ctx, settings)

    assert response.status_code == 422
    assert response.json()["code"] == "empty_config_push"

    # And, critically, refusing it must not have bumped the version anyway.
    got = await _get_device(ctx, settings)
    assert got.json()["desired_state_version"] == 0


async def test_a_negative_value_is_refused_by_the_servers_own_loose_bounds(ctx, settings):
    """The server's own check is deliberately loose (just "must be positive") - this is
    not a duplicate of the agent's exact bounds, only a sanity floor. See `push_config`'s
    own docstring for why."""
    response = await _push_config(ctx, settings, heartbeat_interval_seconds=-5)

    assert response.status_code == 422


async def test_config_push_requires_edge_manage_not_only_edge_read(ctx, settings):
    device = ctx["device"]
    response = await ctx["client"].post(
        f"/api/v1/tenant/edge/devices/{device['device_id']}/config",
        json={"idempotency_key": uuid.uuid4().hex, "heartbeat_interval_seconds": 5},
        headers=_read_only_headers(settings, tenant_id=device["tenant_id"]),
    )

    assert response.status_code == 403


async def test_the_generic_commands_route_refuses_a_config_push_command_type(ctx, settings):
    """Only `POST .../config` may mint a `config_push` command - see `issue_command`'s own
    guard. A caller smuggling this command_type through the generic route would otherwise
    create a `device_commands` row with no `target_version`, which the agent's own `_apply_
    config_push` already refuses - but the server itself refuses it too, so "desired
    state" stays meaningful without relying on the device to be the only enforcement."""
    device = ctx["device"]
    response = await ctx["client"].post(
        f"/api/v1/tenant/edge/devices/{device['device_id']}/commands",
        json={
            "command_type": "config_push", "payload": {"heartbeat_interval_seconds": 5},
            "idempotency_key": uuid.uuid4().hex,
        },
        headers=_owner_headers(settings, tenant_id=device["tenant_id"]),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "use_config_push_route"


# --- Convergence: desired vs. observed, visible on GET /devices/{id} --------------------

async def test_desired_and_observed_converge_once_the_device_reports_back(ctx, settings):
    pushed = await _push_config(ctx, settings, heartbeat_interval_seconds=5)
    target_version = pushed.json()["command"]["payload"]["target_version"]

    before = await _get_device(ctx, settings)
    assert before.json()["desired_state_version"] == target_version
    assert before.json()["observed_state_version"] == 0

    # The device's own next heartbeat, reporting it applied that exact version - the real
    # write path `heartbeat_loop` uses, not a direct database write.
    hb = await _heartbeat(ctx, status="ok", observed_state_version=target_version)
    assert hb.status_code == 200, hb.text

    after = await _get_device(ctx, settings)
    assert after.json()["observed_state_version"] == target_version
    assert after.json()["desired_state_version"] == after.json()["observed_state_version"]


async def test_a_heartbeat_that_omits_observed_state_version_leaves_it_unmoved(ctx, settings):
    """Optional for the same reason spool_depth/spool_dropped are (test_edge_heartbeat_
    spool.py's own docstring): a fleet already in the field, or a build that predates
    config-push, must keep heartbeating exactly as it always has."""
    await _push_config(ctx, settings, heartbeat_interval_seconds=5)
    await _heartbeat(ctx, status="ok")  # no observed_state_version in the body

    got = await _get_device(ctx, settings)
    assert got.json()["observed_state_version"] == 0
    assert got.json()["desired_state_version"] == 1


# --- FLOW-13: expired commands are not executed - proven at the real /pending filter ----

async def test_an_expired_config_push_never_reaches_pending_and_the_gap_stays_visible(ctx, settings):
    """`desired_state_version` is bumped at *issuance*, not at delivery - so a command
    that expires before the device ever polls for it leaves a permanent, visible gap
    (`desired_state_version` ahead of `observed_state_version`), which is the correct
    signal per FLOW-13: this device never converged on what was desired, and never will
    for this particular command."""
    response = await _push_config(ctx, settings, heartbeat_interval_seconds=5, ttl_seconds=30)
    target_version = response.json()["command"]["payload"]["target_version"]

    # Rewind the command's own expiry into the past directly - the same technique test_
    # edge_sync.py's own expired-command test uses at the agent layer, mirrored here at
    # the database layer since `/commands` has no "backdate this" API of its own.
    async with ctx["owner_factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE device_commands SET expires_at = now() - interval '1 minute' "
                 "WHERE edge_device_id = :d"),
            {"d": ctx["device"]["device_id"]},
        )

    pending = await _pending_commands(ctx)
    assert pending.status_code == 200
    assert pending.json() == []  # the real GET /commands/pending filter, not a mock

    got = await _get_device(ctx, settings)
    assert got.json()["desired_state_version"] == target_version
    assert got.json()["observed_state_version"] == 0
