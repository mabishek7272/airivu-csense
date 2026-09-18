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

**Reserve vs. consume**: `reserve_quota()` below only ever writes `consumed_value`
directly, not the two-phase `reserved_value -> consumed_value` flow SCH §6.5 describes for
long-running provisioning. A synchronous, single-transaction resource create (a camera, in
the first caller of this) has no "reserved but not yet created" window to model - it
either commits both together or rolls back both together. `reserve_quota_two_phase()` /
`commit_reservation()` / `release_reservation()` below are that two-phase path, for a
caller with a genuine reserve-now/commit-or-release-later window; `reserve_quota()` itself
stays untouched and remains the right choice for the synchronous case.
"""
from __future__ import annotations

import datetime as dt
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


async def reserve_quota_two_phase(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    quota_code: str,
    quantity: int = 1,
    resource_type: str,
    resource_id: str | None = None,
    idempotency_key: str | None = None,
    expires_at: dt.datetime | None = None,
) -> UUID | None:
    """Locks the tenant's current `quota_ledgers` row, verifies capacity against
    `reserved_value + consumed_value` (same check `reserve_quota()` already does), then
    increments ONLY `reserved_value` and inserts a real `quota_reservations` row with
    `status='reserved'` - the resource itself is NOT created here, unlike `reserve_quota()`.
    Returns the new `quota_reservations.id`, or `None` when no ledger row exists for this
    (tenant, quota_code) - unlimited, same "no row = unlimited" contract `reserve_quota()`
    already establishes. Raises `QuotaExceededError` on the same terms.

    `idempotency_key` lets a caller safely retry a reservation request (e.g. after a
    network timeout on the response, not knowing if the reservation landed) without
    double-reserving - a second call with the same key against an existing
    `status='reserved'` row returns that row's id instead of reserving again.
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
        return None  # no assigned quota for this code on this tenant - unlimited, not denied

    ledger_id, limit_value, reserved_value, consumed_value = row

    if idempotency_key is not None:
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM quota_reservations WHERE quota_ledger_id = :ledger_id "
                    "AND idempotency_key = :idempotency_key AND status = 'reserved'"
                ),
                {"ledger_id": ledger_id, "idempotency_key": idempotency_key},
            )
        ).first()
        if existing is not None:
            return existing[0]

    in_use = reserved_value + consumed_value
    if in_use + quantity > limit_value:
        raise QuotaExceededError(
            quota_code=quota_code, limit_value=limit_value, in_use=in_use, requested=quantity
        )

    await session.execute(
        text(
            "UPDATE quota_ledgers SET reserved_value = reserved_value + :qty, "
            "version = version + 1, updated_at = now() WHERE id = :id"
        ),
        {"qty": quantity, "id": ledger_id},
    )

    return (
        await session.execute(
            text(
                "INSERT INTO quota_reservations "
                "(tenant_id, quota_ledger_id, resource_type, resource_id, idempotency_key, "
                "quantity, status, expires_at) "
                "VALUES (:tenant_id, :ledger_id, :resource_type, :resource_id, :idempotency_key, "
                ":quantity, 'reserved', :expires_at) "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "ledger_id": ledger_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "idempotency_key": idempotency_key,
                "quantity": quantity,
                "expires_at": expires_at,
            },
        )
    ).scalar_one()


async def commit_reservation(session: AsyncSession, *, reservation_id: UUID) -> None:
    """The resource this reservation was for was actually created - moves its quantity
    from `reserved_value` to `consumed_value` on the same `quota_ledgers` row, and flips
    the `quota_reservations` row to `status='committed'`. Idempotent: committing an
    already-committed reservation is a no-op, not an error (a caller retrying after an
    ambiguous failure shouldn't need to first check current status)."""
    ledger_row = (
        await session.execute(
            text("SELECT quota_ledger_id FROM quota_reservations WHERE id = :id"),
            {"id": reservation_id},
        )
    ).first()
    if ledger_row is None:
        return  # no such reservation - nothing to do
    ledger_id = ledger_row[0]

    # Lock the ledger row first, same as reserve_quota_two_phase - this serializes any
    # commit/release racing another reservation against the same ledger.
    await session.execute(
        text("SELECT id FROM quota_ledgers WHERE id = :id FOR UPDATE"), {"id": ledger_id}
    )

    reservation_row = (
        await session.execute(
            text("SELECT quantity, status::text FROM quota_reservations WHERE id = :id FOR UPDATE"),
            {"id": reservation_id},
        )
    ).first()
    quantity, status = reservation_row
    if status != "reserved":
        return  # already committed/released/expired - idempotent no-op

    await session.execute(
        text(
            "UPDATE quota_ledgers SET reserved_value = reserved_value - :qty, "
            "consumed_value = consumed_value + :qty, version = version + 1, updated_at = now() "
            "WHERE id = :id"
        ),
        {"qty": quantity, "id": ledger_id},
    )
    await session.execute(
        text("UPDATE quota_reservations SET status = 'committed', updated_at = now() WHERE id = :id"),
        {"id": reservation_id},
    )


async def release_reservation(session: AsyncSession, *, reservation_id: UUID) -> None:
    """The resource this reservation was for was never created (the caller's own
    long-running process failed, timed out, or was cancelled) - returns its quantity
    from `reserved_value` back to the ledger (never touches `consumed_value`) and flips
    the row to `status='released'`. Idempotent, same reasoning as `commit_reservation()`."""
    ledger_row = (
        await session.execute(
            text("SELECT quota_ledger_id FROM quota_reservations WHERE id = :id"),
            {"id": reservation_id},
        )
    ).first()
    if ledger_row is None:
        return  # no such reservation - nothing to do
    ledger_id = ledger_row[0]

    # Lock the ledger row first, same as reserve_quota_two_phase - this serializes any
    # commit/release racing another reservation against the same ledger.
    await session.execute(
        text("SELECT id FROM quota_ledgers WHERE id = :id FOR UPDATE"), {"id": ledger_id}
    )

    reservation_row = (
        await session.execute(
            text("SELECT quantity, status::text FROM quota_reservations WHERE id = :id FOR UPDATE"),
            {"id": reservation_id},
        )
    ).first()
    quantity, status = reservation_row
    if status != "reserved":
        return  # already released/committed/expired - idempotent no-op

    await session.execute(
        text(
            "UPDATE quota_ledgers SET reserved_value = reserved_value - :qty, "
            "version = version + 1, updated_at = now() WHERE id = :id"
        ),
        {"qty": quantity, "id": ledger_id},
    )
    await session.execute(
        text("UPDATE quota_reservations SET status = 'released', updated_at = now() WHERE id = :id"),
        {"id": reservation_id},
    )
