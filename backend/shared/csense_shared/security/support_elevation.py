"""The DB-touching half of support-grant authorization: given a platform developer and a
target tenant, is there a real, active, unexpired grant that says this developer may act
as that tenant right now - and if so, with which permissions?

Pulled out of `deps.py` (backend/tenant_api/app/deps.py) so it's directly testable against
a real database without a running FastAPI app - the same reasoning
csense_shared.licensing.lifecycle and csense_shared.cameras.health already established.

All the actual authorization logic lives in the `support_grant_lookup()` SQL function
(migration 0051) - a SECURITY DEFINER lookup, callable from a `bootstrap_session()` before
any tenant RLS context exists. This module is a thin, typed wrapper around calling it.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ElevatedGrant:
    grant_id: UUID
    permissions: frozenset[str]


async def elevate_from_grant(
    session: AsyncSession, *, developer_user_id: UUID, tenant_id: UUID
) -> ElevatedGrant | None:
    """Returns the active grant elevating `developer_user_id` into `tenant_id`, or `None`
    if no such grant exists right now. `permissions` is already filtered to real,
    customer-audience-grantable codes - never a raw echo of the grant's own free-text
    `requested_scopes` (see migration 0051's own docstring for why that matters)."""
    row = (
        await session.execute(
            text("SELECT grant_id, permissions FROM support_grant_lookup(:dev, :tenant)"),
            {"dev": developer_user_id, "tenant": tenant_id},
        )
    ).first()
    if row is None:
        return None
    grant_id, permissions = row
    return ElevatedGrant(grant_id=grant_id, permissions=frozenset(permissions or []))
