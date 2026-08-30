"""Concurrent-safe quota reservation - docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §9's
row-lock pattern, applied for real:

  1. Begin database transaction.        (the caller's own tenant_session/bootstrap_session)
  2. Lock the relevant quota ledger row.        (`SELECT ... FOR UPDATE` below)
  3. Verify active entitlement and available quantity.
  4. Reserve/increment use.
  5. Create the resource.               (the caller does this, same transaction)
  6. Commit resource, usage ledger, audit, and outbox together.  (the caller's own commit)

This function only ever does steps 2-4; the caller is responsible for creating the
resource and committing everything together in the same transaction, exactly as every
other write in this codebase already does with `record_audit_and_outbox`.

**No quota_ledgers row for a given (tenant, quota_code) means unlimited - not "0 quota,
deny everything".** This is what lets quota enforcement be wired into an existing,
already-shipped resource-creation flow (see `cameras.py`) without silently breaking every
tenant that existed before licensing did, or any tenant a platform admin/reseller simply
hasn't assigned a plan to yet. A tenant only ever becomes quota-limited once something
actually issues it a license with that entitlement.

**Reserve vs. consume**: this only ever writes `consumed_value` directly, not the
two-phase `reserved_value -> consumed_value` flow SCH §6.5 describes for long-running
provisioning. A synchronous, single-transaction resource create (a camera, in the first
caller of this) has no "reserved but not yet created" window to model - it either commits
both together or rolls back both together. The two-phase path, and `quota_reservations`
rows with `status='reserved'`, are real schema for a future long-running create that
genuinely needs them; nothing in this codebase is that yet.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class QuotaExceededError(Exception):
    """Raised by `reserve_quota` when the requested quantity would exceed the tenant's
    current limit. The caller maps this to whatever HTTP shape its service uses (see
    `cameras.py`'s `camera.count` gate for the Tenant API's own 402 mapping)."""

    def __init__(self, *, quota_code: str, limit_value: int, in_use: int, requested: int) -> None:
        self.quota_code = quota_code
        self.limit_value = limit_value
        self.in_use = in_use
        self.requested = requested
        super().__init__(
            f"{quota_code}: {in_use}/{limit_value} already in use, {requested} more requested"
        )


async def reserve_quota(
    session: AsyncSession, *, tenant_id: UUID, quota_code: str, quantity: int = 1
) -> None:
    """Locks and increments the tenant's current `quota_ledgers` row for `quota_code`,
    inside the caller's own transaction. Raises `QuotaExceededError` if that would exceed
    the row's `limit_value`. No-ops (unlimited) when no such row exists - see module
    docstring.

    "Current" means the row whose period actually covers now - a NULL `period_end` reads
    as open-ended (the common case for a simple running-count entitlement like
    `camera.count`, which SCH §6.4 supports directly rather than needing a distinct
    "count-type quota" concept).
    """
    row = (
        await session.execute(
            text(
                "SELECT id, limit_value, reserved_value, consumed_value FROM quota_ledgers "
                "WHERE tenant_id = :tenant_id AND quota_code = :quota_code "
                "AND period_start <= now() AND (period_end IS NULL OR period_end > now()) "
                "ORDER BY period_start DESC LIMIT 1 FOR UPDATE"
            ),
            {"tenant_id": tenant_id, "quota_code": quota_code},
        )
    ).first()
    if row is None:
        return  # no assigned quota for this code on this tenant - unlimited, not denied

    ledger_id, limit_value, reserved_value, consumed_value = row
    in_use = reserved_value + consumed_value
    if in_use + quantity > limit_value:
        raise QuotaExceededError(
            quota_code=quota_code, limit_value=limit_value, in_use=in_use, requested=quantity
        )

    await session.execute(
        text(
            "UPDATE quota_ledgers SET consumed_value = consumed_value + :qty, "
            "version = version + 1, updated_at = now() WHERE id = :id"
        ),
        {"qty": quantity, "id": ledger_id},
    )
