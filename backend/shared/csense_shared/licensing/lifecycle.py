"""License lifecycle: grace period and expiry, computed lazily on read/use rather than
by a background worker - the same pattern this session already established for support
grants ("an active grant past expires_at flips to expired in the database on the very
next read, not merely hidden" - see `support.py`). A license nobody looks at for a while
does not need a cron job ticking in the background to keep its status honest; the very
next read (the tenant's own `GET /license`, an admin listing, or a resource-creation
quota check) is always soon enough, and there is no user-visible difference between
"expired the instant the clock passed `grace_ends_at`" and "expired the instant something
next asked."

**Grace vs. hard stop.** `grace` is a reduced-friction warning window (SCH's own
`grace_ends_at` column) - the license is stale but the tenant is not cut off yet, so
`require_license_not_restricted` treats it the same as `active`. `expired`, `suspended`,
and `revoked` are hard stops (`RESTRICTED_STATUSES`).

**No license at all is never restricted.** The same "no quota_ledgers row means
unlimited, not zero" fallback `csense_shared.licensing.quota` already documents, for the
same reason: a tenant nobody has assigned a plan to yet must not be blocked by a check
that only exists to enforce a plan someone chose to assign.
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RESTRICTED_STATUSES = ("expired", "suspended", "revoked")


async def sync_license_status(session: AsyncSession, *, license_id: UUID) -> str:
    """Recomputes this license's status against the clock - active -> grace -> expired -
    and persists it if it changed. Returns the up-to-date status either way.

    `FOR UPDATE` locks the row so two concurrent reads racing to flip the same license
    (a tenant's dashboard load and a camera-creation quota check landing at the same
    moment) do not both attempt the write; the second one simply reads what the first
    already committed.
    """
    row = (
        await session.execute(
            text("SELECT status::text, expires_at, grace_ends_at FROM licenses WHERE id = :id FOR UPDATE"),
            {"id": license_id},
        )
    ).first()
    if row is None:
        raise ValueError(f"No such license: {license_id}")
    status, expires_at, grace_ends_at = row

    # Nothing to compute for scheduled (term hasn't started)/suspended/expired/revoked
    # (already a hard stop, and re-deriving from expiry timestamps would be meaningless
    # for a status a human explicitly set) or a term-less license (expires_at IS NULL).
    if status not in ("active", "grace") or expires_at is None:
        return status

    now = dt.datetime.now(dt.UTC)
    new_status = status
    if now >= expires_at:
        new_status = "grace" if (grace_ends_at is not None and now < grace_ends_at) else "expired"

    if new_status != status:
        await session.execute(
            text("UPDATE licenses SET status = :s, updated_at = now() WHERE id = :id"),
            {"s": new_status, "id": license_id},
        )
    return new_status


async def current_license(session: AsyncSession, *, tenant_id: UUID) -> tuple[UUID, str] | None:
    """The tenant's single most relevant license - not filtered by status, deliberately:
    once a license lazily flips to `expired`, it must stay findable by this same lookup
    on the next call, or restriction would silently stop applying the moment nobody was
    looking. `(tenant_id, plan/product scope)` has at most one *effective* (active/grace)
    row at a time (SCH's own constraint, enforced by `issue_license` refusing a second one
    while the first is still active/grace) - so "most recently created" is always either
    that effective row, or the most recent historical one if nothing effective exists.

    Returns `(license_id, freshly-synced status)`, or `None` if this tenant has never
    been issued a license at all.
    """
    row = (
        await session.execute(
            text("SELECT id FROM licenses WHERE tenant_id = :t ORDER BY created_at DESC LIMIT 1"),
            {"t": tenant_id},
        )
    ).first()
    if row is None:
        return None
    license_id = row[0]
    status = await sync_license_status(session, license_id=license_id)
    return license_id, status


async def require_license_not_restricted(session: AsyncSession, *, tenant_id: UUID) -> None:
    """Raises `ApiError(402)` if the tenant's current license has lapsed into a hard-stop
    state. See module docstring for why a tenant with no license at all passes through
    un-restricted, and why `grace` is not itself a restriction."""
    from csense_shared.errors import ApiError

    found = await current_license(session, tenant_id=tenant_id)
    if found is None:
        return
    _, status = found
    if status in RESTRICTED_STATUSES:
        raise ApiError(
            status_code=402, code="license_restricted",
            message=f"This tenant's license is {status}. Contact your reseller or AIRIVU to renew.",
        )
