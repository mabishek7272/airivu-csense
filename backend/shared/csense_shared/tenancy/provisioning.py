"""Creates a brand-new organization + tenant with an *invited* (not active) owner
membership - the common core of two flows that otherwise never share a call path
(TRD §7.2's service isolation):

  - the Admin API's `POST /api/v1/admin/organizations`, a platform admin provisioning any
    organization (a `direct_customer`, or a `reseller`);
  - a reseller's own Tenant API `POST /api/v1/tenant/child-tenants`, provisioning a
    `reseller_customer` organization for one of its downstream customers.

Neither caller knows the new owner's password, unlike `/register` (the person registering
supplies their own password inline, so that flow creates an *active* owner directly). Here
the owner is created `invited` - identical in shape to what `create_invited_membership`
already produces for an *existing* tenant - and is only ever activated through the
existing, unmodified `POST /api/v1/auth/accept-invitation`. That endpoint doesn't care
whether the tenant it's activating a membership in is a minute old or a year old, so
nothing about it needed to change to support this.

Must run inside a caller-managed `bootstrap_session()` (Tenant API) or `platform_session()`
(Admin API) - there is no tenant scope yet because the tenant this function creates doesn't
exist until partway through it. Mirrors `create_organization_tenant_owner`'s identical
need to call `set_tenant_scope` mid-transaction once the new tenant's id exists, since
`memberships` is RLS-protected.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.db.models import Membership, Organization, Role, Tenant, User
from csense_shared.db.postgres import set_tenant_scope


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    return f"{base}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class ProvisionedTenant:
    organization: Organization
    tenant: Tenant
    user: User
    membership: Membership
    owner_is_new_user: bool


async def provision_organization_with_invited_owner(
    session: AsyncSession,
    *,
    organization_type: str,
    organization_name: str,
    owner_email: str,
    owner_display_name: str,
    owner_role: Role,
) -> ProvisionedTenant:
    organization = Organization(
        organization_type=organization_type,
        legal_name=organization_name,
        display_name=organization_name,
        slug=slugify(organization_name),
        status="active",
    )
    session.add(organization)
    await session.flush()  # populate organization.id before tenant needs it

    tenant = Tenant(organization_id=organization.id, status="active")
    session.add(tenant)
    await session.flush()  # populate tenant.id before set_tenant_scope/membership need it

    # memberships is RLS-protected; scope this transaction to the tenant just created -
    # its own boundary, not a bypass of anyone else's (same reasoning
    # create_organization_tenant_owner already documents). Harmless to call again even
    # when the session is already platform-scoped (Admin API's platform_session sets
    # app.is_platform=true, which alone already satisfies every policy this touches).
    await set_tenant_scope(session, tenant.id)

    # An owner email that already has an account elsewhere is reused as-is, not
    # duplicated - the same choice create_invited_membership makes for an existing
    # tenant's invite. Accepting the invitation later sets a fresh password on this user
    # regardless of whether it was just created or already existed; that's an intentional,
    # pre-existing property of accept-invitation (see auth.py), not something introduced
    # here.
    existing = (
        await session.execute(select(User).where(User.email_normalized == owner_email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        user = existing
        owner_is_new_user = False
    else:
        user = User(
            email_normalized=owner_email.lower(), email_display=owner_email,
            password_hash=None, status="invited", display_name=owner_display_name,
        )
        session.add(user)
        await session.flush()
        owner_is_new_user = True

    membership = Membership(
        tenant_id=tenant.id, user_id=user.id, role_id=owner_role.id,
        status="invited", site_scope_mode="all", invited_at=func.now(),
    )
    session.add(membership)
    await session.flush()

    return ProvisionedTenant(
        organization=organization, tenant=tenant, user=user, membership=membership,
        owner_is_new_user=owner_is_new_user,
    )
