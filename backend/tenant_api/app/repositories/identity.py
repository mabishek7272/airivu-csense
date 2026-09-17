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

from sqlalchemy import func, select, text
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
    site_scope_mode: str
    site_ids: frozenset[UUID]


async def get_first_active_membership(session: AsyncSession, user_id: UUID) -> ActiveMembership | None:
    """Resolves the authenticating user's own active membership before tenant scope
    exists. Backed by a SECURITY DEFINER function (migration 0005, extended by 0055 to
    also return site scope) restricted to a single user's own membership - not a general
    cross-tenant query."""
    result = await session.execute(
        text(
            "SELECT membership_id, tenant_id, role_id, site_scope_mode, site_ids "
            "FROM csense_active_membership_for_user(:user_id)"
        ),
        {"user_id": str(user_id)},
    )
    row = result.first()
    if row is None:
        return None
    return ActiveMembership(
        membership_id=row[0], tenant_id=row[1], role_id=row[2],
        site_scope_mode=row[3], site_ids=frozenset(row[4] or []),
    )


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
        # An owner always gets full access to their own tenant's sites - scoping
        # yourself out of the sites in the tenant you just created is not a real
        # scenario (see docs/superpowers/plans/2026-09-17-licensing-rbac-reseller-
        # features.md, "Before you start"). Without this, the row falls through to
        # the schema default `site_scope_mode="none"` (zero sites) while
        # register()'s freshly issued token claims "all" - a mismatch that's
        # latent today (nothing enforces site scope yet) but becomes a silent
        # owner lockout the moment site-scope enforcement ships.
        site_scope_mode="all",
    )
    session.add(membership)
    await session.flush()
    return membership


async def count_active_owners(session: AsyncSession, tenant_id: UUID) -> int:
    """For the "don't lock a tenant out of its own account" guard - `memberships.py`
    refuses a revoke/role-change that would bring this to zero."""
    owner_role = await get_role_by_name(session, "tenant_owner", "customer")
    result = await session.execute(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.role_id == owner_role.id,
            Membership.status == "active",
        )
    )
    return len(result.all())


async def create_invited_membership(
    session: AsyncSession, *, tenant_id: UUID, email: str, display_name: str, role: Role,
    site_scope_mode: str, invited_by: UUID,
) -> tuple[User, Membership]:
    """Runs inside an already tenant-scoped session (`tenant_session()`, the same as any
    other write in this API) - unlike `create_organization_tenant_owner`, there is no
    tenant to create here, so no mid-transaction scope change is needed.

    A brand-new email gets a real `User` row with `status='invited'` and
    `password_hash=None` - the schema already treats both as first-class values (not a
    placeholder hack), and `login()`'s own checks (`user.status != "active"`, plus its
    dummy-hash comparison when `password_hash` is falsy) already refuse it correctly with
    no extra code needed here. An email that already has an account is reused as-is - a
    second membership for an existing user, not a second identity.
    """
    existing = await get_user_by_email(session, email)
    if existing is not None:
        user = existing
    else:
        user = User(
            email_normalized=email.lower(), email_display=email,
            password_hash=None, status="invited", display_name=display_name,
        )
        session.add(user)
        await session.flush()

    membership = Membership(
        tenant_id=tenant_id, user_id=user.id, role_id=role.id,
        status="invited", site_scope_mode=site_scope_mode,
        invited_by=invited_by, invited_at=func.now(),
    )
    session.add(membership)
    await session.flush()
    return user, membership


async def activate_membership(
    session: AsyncSession, *, membership: Membership, user: User, password_hash: str,
) -> None:
    """The accept-invitation counterpart to `create_invited_membership` - sets the real
    password, flips both the user and the membership from their `invited` placeholders to
    real, usable ones."""
    user.password_hash = password_hash
    user.status = "active"
    user.email_verified_at = func.now()
    membership.status = "active"
    membership.accepted_at = func.now()
    await session.flush()
