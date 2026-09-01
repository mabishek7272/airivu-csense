"""elevate_from_grant() - the DB-touching half of support-grant authorization.

Needs a migrated database (migration 0051); skipped otherwise, same convention as
test_license_lifecycle.py.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.db.postgres import bootstrap_session
from csense_shared.security.support_elevation import elevate_from_grant

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _async_dsn(env_var: str = "TEST_POSTGRES_DSN") -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
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
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Support Elevation Test {suffix}", "s": f"support-elevation-test-{suffix}"},
            )
        ).scalar_one()
        # tenants.organization_id is unique (migration 0001: one tenant per organization),
        # so the "other tenant" needs its own organization rather than reusing org_id.
        other_org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Support Elevation Test Other {suffix}", "s": f"support-elevation-test-other-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        other_tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": other_org_id},
            )
        ).scalar_one()
        developer_email = f"support-elevation-{suffix}@example.test"
        developer_id = (
            await session.execute(
                text(
                    "INSERT INTO users (email_normalized, email_display, display_name, status) "
                    "VALUES (:e1, :e2, 'Test Developer', 'active') RETURNING id"
                ),
                # Two separate params, not one reused: email_normalized is citext and
                # email_display is text, and asyncpg can't deduce a single consistent type
                # for one bind parameter used at two differently-typed positions.
                {"e1": developer_email, "e2": developer_email},
            )
        ).scalar_one()

    yield {"factory": factory, "tenant_id": tenant_id, "other_tenant_id": other_tenant_id, "developer_id": developer_id}

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM support_grants WHERE developer_user_id = :d"), {"d": developer_id})
        await session.execute(text("DELETE FROM tenants WHERE id IN (:t, :o)"), {"t": tenant_id, "o": other_tenant_id})
        await session.execute(
            text("DELETE FROM organizations WHERE id IN (:o, :oo)"), {"o": org_id, "oo": other_org_id}
        )
        await session.execute(text("DELETE FROM users WHERE id = :d"), {"d": developer_id})

    await engine.dispose()


async def _make_grant(ctx, **overrides) -> uuid.UUID:
    params = {
        "developer_user_id": ctx["developer_id"],
        "tenant_id": ctx["tenant_id"],
        "ticket_reference": "TICKET-1",
        "purpose": "Investigating a customer-reported issue that needs real data access.",
        "requested_scopes": ["incident.read", "bogus.nonexistent.permission"],
        "status": "active",
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(hours=8),
    }
    params.update(overrides)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        grant_id = (
            await session.execute(
                text(
                    "INSERT INTO support_grants "
                    "(developer_user_id, tenant_id, ticket_reference, purpose, requested_scopes, status, expires_at) "
                    "VALUES (:developer_user_id, :tenant_id, :ticket_reference, :purpose, :requested_scopes, "
                    " CAST(:status AS support_grant_status), :expires_at) RETURNING id"
                ),
                params,
            )
        ).scalar_one()
    return grant_id


@pytest.mark.asyncio
async def test_no_grant_returns_none(ctx):
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(db, developer_user_id=ctx["developer_id"], tenant_id=ctx["tenant_id"])
    assert result is None


@pytest.mark.asyncio
async def test_active_grant_elevates_with_scopes_filtered_to_real_customer_permissions(ctx):
    grant_id = await _make_grant(ctx)
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(db, developer_user_id=ctx["developer_id"], tenant_id=ctx["tenant_id"])
    assert result is not None
    assert result.grant_id == grant_id
    # 'bogus.nonexistent.permission' was never a real permission code - it must not survive.
    assert result.permissions == frozenset({"incident.read"})


@pytest.mark.asyncio
async def test_requested_status_does_not_elevate(ctx):
    await _make_grant(ctx, status="requested")
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(db, developer_user_id=ctx["developer_id"], tenant_id=ctx["tenant_id"])
    assert result is None


@pytest.mark.asyncio
async def test_revoked_grant_does_not_elevate(ctx):
    await _make_grant(ctx, status="revoked")
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(db, developer_user_id=ctx["developer_id"], tenant_id=ctx["tenant_id"])
    assert result is None


@pytest.mark.asyncio
async def test_expired_grant_does_not_elevate_even_if_status_column_still_says_active(ctx):
    # Lazy expiry: nothing has flipped `status` yet, but expires_at has already passed.
    await _make_grant(ctx, expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(db, developer_user_id=ctx["developer_id"], tenant_id=ctx["tenant_id"])
    assert result is None


@pytest.mark.asyncio
async def test_grant_for_a_different_tenant_does_not_elevate(ctx):
    await _make_grant(ctx)
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(
            db, developer_user_id=ctx["developer_id"], tenant_id=ctx["other_tenant_id"]
        )
    assert result is None


@pytest.mark.asyncio
async def test_grant_for_a_different_developer_does_not_elevate(ctx):
    await _make_grant(ctx)
    async with bootstrap_session(ctx["factory"]) as db:
        result = await elevate_from_grant(
            db, developer_user_id=uuid.uuid4(), tenant_id=ctx["tenant_id"]
        )
    assert result is None
