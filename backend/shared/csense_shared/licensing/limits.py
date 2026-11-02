"""Read a numeric limit off a tenant's effective licence.

Unlike `quota.reserve_quota` (a ledger that counts things being created and consumed),
these limits describe a *concurrent* state - phones currently signed in, browsers open right
now - so the caller counts what is live itself and only asks this module for the ceiling.

`None` means "no limit": no effective licence, or a licence whose plan does not carry this
entitlement. That is the same "unassigned means unlimited, never zero" stance
`licensing/quota.py` and `licensing/lifecycle.py` take, so adding a new entitlement code
never locks out a tenant that predates it.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MOBILE_DEVICE_LIMIT = "mobile_device.count"
WEB_SESSION_LIMIT = "web_session.concurrent"

_LIMIT_QUERY = text(
    """
    SELECT e.limit_numeric
    FROM license_entitlements e
    JOIN licenses l ON l.id = e.license_id
    WHERE e.tenant_id = :tenant_id
      AND e.entitlement_code = :code
      AND e.value_type = 'limit_numeric'
      AND l.status IN ('active', 'grace')
      AND e.effective_from <= now()
      AND (e.effective_to IS NULL OR e.effective_to > now())
    ORDER BY e.effective_from DESC
    LIMIT 1
    """
)


async def entitlement_limit(session: AsyncSession, tenant_id: UUID, code: str) -> int | None:
    """The tenant's ceiling for `code`, or None when there is none.

    The session must already be scoped to `tenant_id` (RLS on `license_entitlements`).
    """
    value = (await session.execute(_LIMIT_QUERY, {"tenant_id": tenant_id, "code": code})).scalar_one_or_none()
    return int(value) if value is not None else None
