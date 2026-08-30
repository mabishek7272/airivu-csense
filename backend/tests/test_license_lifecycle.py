"""License grace/expiry lifecycle (csense_shared.licensing.lifecycle) - the lazy
active -> grace -> expired transition, and the restriction check built on it.

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

from csense_shared.errors import ApiError
from csense_shared.licensing.lifecycle import (
    current_license,
    require_license_not_restricted,
    sync_license_status,
)

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
                {"n": f"License Lifecycle Test {suffix}", "s": f"license-lifecycle-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        plan_id = (
            await session.execute(
                text(
                    "INSERT INTO license_plans (code, name, license_type, billing_period) "
                    "VALUES (:c, 'Test Plan', 'standard', 'yearly') RETURNING id"
                ),
                {"c": f"test-plan-{suffix}"},
            )
        ).scalar_one()

    yield {"factory": factory, "tenant_id": tenant_id, "org_id": org_id, "plan_id": plan_id}

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await session.execute(text("DELETE FROM license_plans WHERE id = :p"), {"p": plan_id})

    await engine.dispose()


async def _make_license(ctx, **overrides) -> uuid.UUID:
    params = {
        "tenant_id": ctx["tenant_id"], "organization_id": ctx["org_id"], "plan_id": ctx["plan_id"],
        "status": "active", "expires_at": None, "grace_ends_at": None,
    }
    params.update(overrides)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        license_id = (
            await session.execute(
                text(
                    "INSERT INTO licenses (tenant_id, organization_id, plan_id, status, expires_at, grace_ends_at) "
                    "VALUES (:tenant_id, :organization_id, :plan_id, CAST(:status AS license_status), "
                    ":expires_at, :grace_ends_at) RETURNING id"
                ),
                params,
            )
        ).scalar_one()
    return license_id


def _ago(**kwargs) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(**kwargs)


def _from_now(**kwargs) -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(**kwargs)


async def test_a_license_still_within_its_term_stays_active(ctx):
    license_id = await _make_license(ctx, expires_at=_from_now(days=30))
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "active"


async def test_a_license_past_expiry_with_a_grace_window_flips_to_grace(ctx):
    license_id = await _make_license(
        ctx, expires_at=_ago(days=1), grace_ends_at=_from_now(days=13),
    )
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "grace"


async def test_the_flip_to_grace_is_actually_persisted_not_just_returned(ctx):
    license_id = await _make_license(ctx, expires_at=_ago(days=1), grace_ends_at=_from_now(days=13))
    async with ctx["factory"]() as session, session.begin():
        await sync_license_status(session, license_id=license_id)

    async with ctx["factory"]() as session, session.begin():
        row = (
            await session.execute(text("SELECT status::text FROM licenses WHERE id = :id"), {"id": license_id})
        ).first()
    assert row[0] == "grace"


async def test_a_license_past_its_grace_window_flips_to_expired(ctx):
    license_id = await _make_license(ctx, expires_at=_ago(days=20), grace_ends_at=_ago(days=1))
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "expired"


async def test_a_license_past_expiry_with_no_grace_window_flips_straight_to_expired(ctx):
    license_id = await _make_license(ctx, expires_at=_ago(days=1), grace_ends_at=None)
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "expired"


async def test_a_term_less_license_never_expires(ctx):
    license_id = await _make_license(ctx, expires_at=None)
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "active"


async def test_a_suspended_license_is_left_alone_by_the_clock(ctx):
    """A human explicitly suspended this - re-deriving from expires_at would silently
    overrule that decision the next time anything reads it."""
    license_id = await _make_license(ctx, status="suspended", expires_at=_ago(days=1))
    async with ctx["factory"]() as session, session.begin():
        status = await sync_license_status(session, license_id=license_id)
    assert status == "suspended"


async def test_current_license_finds_the_most_recent_row_regardless_of_status(ctx):
    """Once a license lazily flips to expired, it must stay findable by the next call -
    not silently read back as "no license" (see the module's own docstring)."""
    license_id = await _make_license(ctx, expires_at=_ago(days=20), grace_ends_at=_ago(days=1))
    async with ctx["factory"]() as session, session.begin():
        found = await current_license(session, tenant_id=ctx["tenant_id"])
    assert found == (license_id, "expired")


async def test_current_license_is_none_for_a_tenant_never_issued_one(ctx):
    async with ctx["factory"]() as session, session.begin():
        found = await current_license(session, tenant_id=ctx["tenant_id"])
    assert found is None


async def test_restriction_passes_a_tenant_with_no_license_at_all(ctx):
    async with ctx["factory"]() as session, session.begin():
        await require_license_not_restricted(session, tenant_id=ctx["tenant_id"])  # must not raise


async def test_restriction_passes_a_tenant_in_grace(ctx):
    await _make_license(ctx, expires_at=_ago(days=1), grace_ends_at=_from_now(days=13))
    async with ctx["factory"]() as session, session.begin():
        await require_license_not_restricted(session, tenant_id=ctx["tenant_id"])  # must not raise


async def test_restriction_refuses_a_tenant_past_grace(ctx):
    await _make_license(ctx, expires_at=_ago(days=20), grace_ends_at=_ago(days=1))
    async with ctx["factory"]() as session, session.begin():
        with pytest.raises(ApiError) as exc_info:
            await require_license_not_restricted(session, tenant_id=ctx["tenant_id"])
    assert exc_info.value.status_code == 402
    assert exc_info.value.code == "license_restricted"


async def test_restriction_refuses_a_suspended_tenant(ctx):
    await _make_license(ctx, status="suspended")
    async with ctx["factory"]() as session, session.begin():
        with pytest.raises(ApiError) as exc_info:
            await require_license_not_restricted(session, tenant_id=ctx["tenant_id"])
    assert exc_info.value.code == "license_restricted"
