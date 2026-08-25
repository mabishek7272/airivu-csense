"""Postgres async engine/session plumbing with mandatory RLS session-var binding.

SCH §15 / TRD-SEC-002: every tenant-owned table has `FORCE ROW LEVEL SECURITY` with a
policy of the shape (see migration 0005):

    tenant_id = current_setting('app.tenant_id', true)::uuid
    OR (
        current_setting('app.is_platform', true)::boolean
        AND pg_has_role(current_user, 'csense_platform', 'MEMBER')
    )

Two things gate the cross-tenant bypass, deliberately:

  - the per-transaction `app.is_platform` opt-in, set only by `platform_session()`; and
  - membership in the `csense_platform` database role.

The Tenant API connects as a role that is *not* in that group, so it cannot read across
tenants even if it sets the flag itself — the isolation boundary does not depend on
application code being free of injection bugs. Note also that none of the application
roles may be a PostgreSQL superuser: superusers bypass RLS unconditionally, which would
render every policy here inert. `bootstrap_roles.py` enforces that at startup and
`tests/test_tenant_isolation.py` asserts it.

Every helper below scopes its settings to the transaction (`set_config(..., true)`), so
pooled connections never leak tenant scope between requests. Connections that call no
helper at all see no rows in tenant-owned tables, which is the fail-safe default
(architecture principle 9).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from csense_shared.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.postgres_dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        async with session.begin():
            # `SET LOCAL x = :param` is a syntax error — PostgreSQL's SET does not accept
            # bind parameters. set_config(name, value, is_local=true) is the parameterized
            # equivalent, and keeps the value transaction-local so pooled connections
            # never leak tenant scope between requests.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await session.execute(text("SELECT set_config('app.is_platform', 'false', true)"))
            yield session


@asynccontextmanager
async def bootstrap_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A transaction with NO tenant scope set yet, for the auth flows that legitimately
    run before a tenant context exists (registration, login).

    Because no `app.tenant_id` is set, RLS-protected tables return zero rows and reject
    inserts — that is intentional. Registration calls `set_tenant_scope()` mid-transaction
    once it has created the tenant; login uses the narrow
    `csense_active_membership_for_user()` SECURITY DEFINER lookup (migration 0005) rather
    than any blanket bypass. Neither path grants cross-tenant reach.
    """
    async with session_factory() as session:
        async with session.begin():
            yield session


async def set_tenant_scope(session: AsyncSession, tenant_id: UUID) -> None:
    """Applies tenant scope to an in-progress transaction (see `bootstrap_session`).

    Transaction-local, so it is discarded on commit/rollback and cannot leak to the next
    checkout of this pooled connection.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@asynccontextmanager
async def platform_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """For Admin API privileged repositories only (TRD §7.2: "platform queries use a
    separate privileged repository interface and cannot be reached from tenant API code
    paths"). Every use of this must be paired with an explicit permission check in the
    caller — this only grants DB-row visibility, not authorization.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
            yield session
