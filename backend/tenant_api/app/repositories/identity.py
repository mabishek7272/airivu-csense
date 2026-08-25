"""Identity repository for the Tenant API's own auth bootstrap flows.

Registration and login run in `bootstrap_session()` — a transaction with no tenant scope
set yet, because by definition none exists while a user is authenticating or a new tenant
is being provisioned. The Tenant API's database role has no RLS bypass at all, so:

  - registration sets tenant scope mid-transaction (`set_tenant_scope`) as soon as it has
    created the tenant, and inserts the membership under that scope like any other write;
  - login resolves "which tenant does this user belong to" through
    `csense_active_membership_for_user()`, a narrow SECURITY DEFINER function (migration
    0005) that returns only that one user's own active membership.

Neither path can reach another tenant's data.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.db.models import Membership, Organization, Permission, Role, RolePermission, Tenant, User
from csense_shared.db.postgres import set_tenant_scope


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email_normalized == email.lower()))
    return result.scalar_one_or_none()


async def get_role_by_name(session: AsyncSession, name: str, audience: str) -> Role:
    result = await session.execute(
        select(Role).where(Role.tenant_id.is_(None), Role.name == name, Role.audience == audience)
    )
    return result.scalar_one()


@dataclass(frozen=True)
class ActiveMembership:
    membership_id: UUID
    tenant_id: UUID
    role_id: UUID


async def get_first_active_membership(session: AsyncSession, user_id: UUID) -> ActiveMembership | None:
    """Resolves the authenticating user's own active membership before tenant scope
    exists. Backed by a SECURITY DEFINER function (migration 0005) restricted to a single
    user's own membership — not a general cross-tenant query."""
    result = await session.execute(
        text(
            "SELECT membership_id, tenant_id, role_id "
            "FROM csense_active_membership_for_user(:user_id)"
        ),
        {"user_id": str(user_id)},
    )
    row = result.first()
    if row is None:
        return None
    return ActiveMembership(membership_id=row[0], tenant_id=row[1], role_id=row[2])


async def get_role_permissions(session: AsyncSession, role_id: UUID) -> frozenset[str]:
    result = await session.execute(
        select(Permission.code, RolePermission.effect)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    rows = result.all()
    allowed = {code for code, effect in rows if effect == "allow"}
    denied = {code for code, effect in rows if effect == "deny"}
    return frozenset(allowed - denied)


async def create_organization_tenant_owner(
    session: AsyncSession,
    *,
    organization: Organization,
    tenant: Tenant,
    user: User,
    owner_role: Role,
) -> Membership:
    session.add(organization)
    session.add(user)
    await session.flush()  # populate organization.id / user.id before dependent inserts

    tenant.organization_id = organization.id
    session.add(tenant)
    await session.flush()  # populate tenant.id before the membership row needs it

    # memberships is RLS-protected and this transaction has no tenant scope yet, so the
    # insert below would be rejected. Scope the transaction to the tenant we just created
    # — the new tenant's own boundary, not a bypass of anyone else's.
    await set_tenant_scope(session, tenant.id)

    membership = Membership(
        tenant_id=tenant.id,
        user_id=user.id,
        role_id=owner_role.id,
        status="active",
        accepted_at=None,
    )
    session.add(membership)
    await session.flush()
    return membership
