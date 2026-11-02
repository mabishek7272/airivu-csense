"""Which phones/tablets are signed in for a tenant, and the licence limit on them.

A device is the app's own install id, so signing in again on the same phone never uses a
second slot. Only a *new* device (or one whose slot was revoked) is checked against
`mobile_device.count`; a tenant with no such entitlement is unlimited.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.errors import ApiError


async def admit_device(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    device_id: str,
    name: str | None,
    platform: str | None,
    limit: int | None,
) -> str | None:
    """Registers or refreshes the device, or refuses it when the tenant is at its limit.

    Returns the device's previous refresh-session id (so the caller can sign that session
    out: one live session per device), or None. The session must be tenant-scoped.

    Serialised per tenant with an advisory lock so two phones signing in at the same
    moment cannot both take the last slot.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"mobile_devices:{tenant_id}"})

    existing = (
        await db.execute(
            text("SELECT id, revoked_at, session_id FROM mobile_devices WHERE device_id = :d"),
            {"d": device_id},
        )
    ).first()

    if existing is not None and existing[1] is None:
        await db.execute(
            text(
                "UPDATE mobile_devices SET last_seen_at = now(), user_id = :u, "
                "name = coalesce(:n, name), platform = coalesce(:p, platform) WHERE id = :id"
            ),
            {"u": user_id, "n": name, "p": platform, "id": existing[0]},
        )
        return existing[2]

    if limit is not None:
        active = (
            await db.execute(text("SELECT count(*) FROM mobile_devices WHERE revoked_at IS NULL"))
        ).scalar_one()
        if active >= limit:
            raise ApiError(
                status_code=403,
                code="device_limit_reached",
                message=(
                    f"This plan allows {limit} signed-in device{'s' if limit != 1 else ''} and all "
                    f"{'are' if limit != 1 else 'is'} in use. Ask your account owner to remove one "
                    "under Settings > Devices, then sign in again."
                ),
                details={"limit": limit},
            )

    if existing is None:
        await db.execute(
            text(
                "INSERT INTO mobile_devices (tenant_id, user_id, device_id, name, platform) "
                "VALUES (:t, :u, :d, :n, :p)"
            ),
            {"t": tenant_id, "u": user_id, "d": device_id, "n": name, "p": platform},
        )
        return None

    await db.execute(
        text(
            "UPDATE mobile_devices SET revoked_at = NULL, last_seen_at = now(), user_id = :u, "
            "name = coalesce(:n, name), platform = coalesce(:p, platform) WHERE id = :id"
        ),
        {"u": user_id, "n": name, "p": platform, "id": existing[0]},
    )
    return existing[2]
