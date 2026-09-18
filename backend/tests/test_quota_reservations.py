"""Two-phase quota reservation (csense_shared.licensing.quota) - reserve_quota_two_phase,
commit_reservation, release_reservation. Modeled on test_license_lifecycle.py's fixture
pattern: real Postgres, a real org/tenant/plan/license/quota_ledgers row per test.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.licensing.quota import (
    QuotaExceededError,
    commit_reservation,
    release_reservation,
    reserve_quota_two_phase,
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
                {"n": f"Quota Reservation Test {suffix}", "s": f"quota-reservation-test-{suffix}"},
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
        license_id = (
            await session.execute(
                text(
                    "INSERT INTO licenses (tenant_id, organization_id, plan_id, status) "
                    "VALUES (:tenant_id, :organization_id, :plan_id, CAST('active' AS license_status)) "
                    "RETURNING id"
                ),
                {"tenant_id": tenant_id, "organization_id": org_id, "plan_id": plan_id},
            )
        ).scalar_one()

    yield {
        "factory": factory,
        "tenant_id": tenant_id,
        "org_id": org_id,
        "plan_id": plan_id,
        "license_id": license_id,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        # tenant_id FK cascades: licenses -> quota_ledgers -> quota_reservations
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await session.execute(text("DELETE FROM license_plans WHERE id = :p"), {"p": plan_id})

    await engine.dispose()


async def _make_ledger(ctx, *, quota_code: str, limit_value: int, **overrides) -> uuid.UUID:
    params = {
        "tenant_id": ctx["tenant_id"],
        "license_id": ctx["license_id"],
        "quota_code": quota_code,
        "limit_value": limit_value,
        "reserved_value": 0,
        "consumed_value": 0,
    }
    params.update(overrides)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        ledger_id = (
            await session.execute(
                text(
                    "INSERT INTO quota_ledgers "
                    "(tenant_id, license_id, quota_code, limit_value, reserved_value, consumed_value) "
                    "VALUES (:tenant_id, :license_id, :quota_code, :limit_value, :reserved_value, :consumed_value) "
                    "RETURNING id"
                ),
                params,
            )
        ).scalar_one()
    return ledger_id


async def _ledger_values(ctx, ledger_id: uuid.UUID) -> tuple[int, int]:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        row = (
            await session.execute(
                text("SELECT reserved_value, consumed_value FROM quota_ledgers WHERE id = :id"),
                {"id": ledger_id},
            )
        ).first()
    return (row[0], row[1])


async def _reservation_row(ctx, reservation_id: uuid.UUID) -> tuple[str, int, uuid.UUID]:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        row = (
            await session.execute(
                text(
                    "SELECT status::text, quantity, quota_ledger_id FROM quota_reservations WHERE id = :id"
                ),
                {"id": reservation_id},
            )
        ).first()
    return (row[0], row[1], row[2])


async def test_reserve_increments_reserved_value_not_consumed(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=3,
            resource_type="camera",
        )

    assert reservation_id is not None
    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 3
    assert consumed_value == 0

    status, quantity, row_ledger_id = await _reservation_row(ctx, reservation_id)
    assert status == "reserved"
    assert quantity == 3
    assert row_ledger_id == ledger_id


async def test_reserve_respects_the_same_capacity_check_as_reserve_quota(ctx):
    ledger_id = await _make_ledger(
        ctx, quota_code="camera.count", limit_value=5, reserved_value=2, consumed_value=2
    )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        with pytest.raises(QuotaExceededError) as exc_info:
            await reserve_quota_two_phase(
                session,
                tenant_id=ctx["tenant_id"],
                quota_code="camera.count",
                quantity=2,
                resource_type="camera",
            )

    assert exc_info.value.quota_code == "camera.count"
    assert exc_info.value.limit_value == 5
    assert exc_info.value.in_use == 4
    assert exc_info.value.requested == 2

    # rolled back - nothing was reserved
    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 2
    assert consumed_value == 2


async def test_reserve_with_no_ledger_row_returns_none_unlimited(ctx):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=1,
            resource_type="camera",
        )

    assert reservation_id is None


async def test_commit_moves_quantity_from_reserved_to_consumed(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=4,
            resource_type="camera",
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await commit_reservation(session, reservation_id=reservation_id)

    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 0
    assert consumed_value == 4

    status, quantity, _ = await _reservation_row(ctx, reservation_id)
    assert status == "committed"
    assert quantity == 4


async def test_commit_is_idempotent_on_an_already_committed_reservation(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=4,
            resource_type="camera",
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await commit_reservation(session, reservation_id=reservation_id)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await commit_reservation(session, reservation_id=reservation_id)  # must not raise or double-move

    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 0
    assert consumed_value == 4  # not 8

    status, quantity, _ = await _reservation_row(ctx, reservation_id)
    assert status == "committed"
    assert quantity == 4


async def test_release_returns_quantity_to_the_ledger_without_touching_consumed(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10, consumed_value=3)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await release_reservation(session, reservation_id=reservation_id)

    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 0
    assert consumed_value == 3  # untouched

    status, quantity, _ = await _reservation_row(ctx, reservation_id)
    assert status == "released"
    assert quantity == 2


async def test_release_is_idempotent_on_an_already_released_reservation(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10, consumed_value=3)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        reservation_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await release_reservation(session, reservation_id=reservation_id)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await release_reservation(session, reservation_id=reservation_id)  # must not raise or double-return

    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 0  # not negative
    assert consumed_value == 3

    status, _, _ = await _reservation_row(ctx, reservation_id)
    assert status == "released"


async def test_idempotency_key_returns_the_existing_reservation_not_a_second_one(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=10)
    key = "retry-after-timeout-1"

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        first_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
            idempotency_key=key,
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        second_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
            idempotency_key=key,
        )

    assert first_id is not None
    assert second_id == first_id

    reserved_value, _ = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 2  # not 4 - the retry did not reserve again


async def test_reserve_then_release_then_reserve_again_succeeds(ctx):
    ledger_id = await _make_ledger(ctx, quota_code="camera.count", limit_value=2)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        first_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
        )

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await release_reservation(session, reservation_id=first_id)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        second_id = await reserve_quota_two_phase(
            session,
            tenant_id=ctx["tenant_id"],
            quota_code="camera.count",
            quantity=2,
            resource_type="camera",
        )

    assert second_id is not None
    assert second_id != first_id

    reserved_value, consumed_value = await _ledger_values(ctx, ledger_id)
    assert reserved_value == 2
    assert consumed_value == 0
