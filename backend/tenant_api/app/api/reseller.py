"""A reseller organization's own child tenants (TRD §8.2: "A reseller is an organization
with reseller entitlements. Child tenants remain independent tenant boundaries. Reseller
aggregate views are computed from authorized child relationships; records are not stored
in a shared reseller tenant.").

`reseller.manage_children` (migration 0037) is granted broadly to every `tenant_owner`,
the same way `membership.manage` is - the permission alone only establishes "this person
owns some tenant". The actual business rule, "only a reseller organization may create or
list child tenants", is enforced here by checking `organizations.organization_type` for
the caller's own tenant, refusing a non-reseller caller with a clear `403 not_a_reseller`
rather than a bare permission-denied.

A child tenant's owner is created *invited*, exactly like `provision_organization_with_
invited_owner` (shared with the Admin API's own organization creation) always does - the
reseller supplies only the new customer's email and name, never a password, so the new
owner activates through the existing, unmodified `POST /api/v1/auth/accept-invitation`.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from app.repositories.identity import get_role_by_name
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.db.models import OrganizationRelationship
from csense_shared.errors import ApiError
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.invitation_tickets import create_invitation_ticket
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext
from csense_shared.tenancy import provision_organization_with_invited_owner

router = APIRouter(prefix="/api/v1/tenant/child-tenants", tags=["reseller"])


async def _require_reseller_organization(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Returns the caller's own organization id, refusing anything but a reseller."""
    row = (
        await db.execute(
            text(
                "SELECT o.id, o.organization_type FROM organizations o "
                "JOIN tenants t ON t.organization_id = o.id WHERE t.id = :tid"
            ),
            {"tid": tenant_id},
        )
    ).first()
    if row is None or row[1] != "reseller":
        raise ApiError(
            status_code=403,
            code="not_a_reseller",
            message="Only a reseller organization can manage child tenants.",
        )
    return row[0]


class ChildTenantOut(BaseModel):
    tenant_id: str
    organization_id: str
    display_name: str
    status: str
    created_at: str


# Deliberately no join to memberships/users: memberships is RLS-protected per its own
# tenant (migration 0001), and this query runs under the reseller's own tenant scope, not
# the child's - a join to the child's membership rows would be silently emptied by RLS
# (WITH CHECK requires tenant_id = current app.tenant_id OR app.is_platform), producing a
# child tenant that mysteriously vanished from its own parent's list rather than a clear
# error. TRD §8.2's "reseller aggregate views are computed from authorized child
# relationships" is exactly this - organizations/tenants/organization_relationships carry
# no RLS at all, so this reads only what a reseller is actually authorized to see: which
# child tenants exist, not their internal membership rows. Owner email is returned once,
# at creation time, from data already in hand - not re-queried here.
_LIST_SQL = """
    SELECT t.id, o.id, o.display_name, t.status, t.created_at
    FROM organization_relationships rel
    JOIN organizations o ON o.id = rel.child_organization_id
    JOIN tenants t ON t.organization_id = o.id
    WHERE rel.parent_organization_id = :parent_id AND rel.status = 'active'
    ORDER BY t.created_at DESC
"""


@router.get("", response_model=list[ChildTenantOut])
async def list_child_tenants(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ChildTenantOut]:
    require_permission(context, "reseller.manage_children")
    parent_organization_id = await _require_reseller_organization(db, context.tenant_id)

    rows = (await db.execute(text(_LIST_SQL), {"parent_id": parent_organization_id})).all()
    return [
        ChildTenantOut(
            tenant_id=str(r[0]), organization_id=str(r[1]), display_name=r[2],
            status=r[3], created_at=r[4].isoformat(),
        )
        for r in rows
    ]


class CreateChildTenantIn(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    owner_email: EmailStr
    owner_display_name: str = Field(min_length=1, max_length=200)


class CreateChildTenantOut(ChildTenantOut):
    # Known from the request just handled, not re-queried - see _LIST_SQL's docstring for
    # why the list endpoint itself can't cheaply re-derive this.
    owner_email: str
    # Only set when the invitation email could not actually be sent - see
    # memberships.py's invite_member for the same reasoning applied here.
    invitation_link: str | None = None


@router.post("", response_model=CreateChildTenantOut, status_code=201)
async def create_child_tenant(
    body: CreateChildTenantIn,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> CreateChildTenantOut:
    require_permission(context, "reseller.manage_children")
    parent_organization_id = await _require_reseller_organization(db, context.tenant_id)

    # Recorded under the reseller's own tenant *before* provisioning switches this
    # transaction's RLS scope to the new child tenant (provision_organization_with_
    # invited_owner calls set_tenant_scope mid-transaction, same as
    # create_organization_tenant_owner already does for /register) - after that switch,
    # a write scoped to the reseller's own tenant_id would be rejected by RLS.
    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        action="reseller.child_tenant.create",
        outcome="success",
        target_type="organization",
        target_id=None,  # the child doesn't exist yet at this point in the transaction
        reason=f"Creating child tenant '{body.organization_name}' for {body.owner_email}",
        before_patch=None,
        after_patch={"organization_name": body.organization_name, "owner_email": body.owner_email},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="reseller.child_tenant.creating.v1",
        event_payload={"organization_name": body.organization_name, "owner_email": body.owner_email},
        aggregate_type="tenant",
        aggregate_id=str(context.tenant_id),
    )

    owner_role = await get_role_by_name(db, "tenant_owner", "customer")
    provisioned = await provision_organization_with_invited_owner(
        db,
        organization_type="reseller_customer",
        organization_name=body.organization_name,
        owner_email=body.owner_email,
        owner_display_name=body.owner_display_name,
        owner_role=owner_role,
    )

    relationship = OrganizationRelationship(
        parent_organization_id=parent_organization_id,
        child_organization_id=provisioned.organization.id,
        relationship_type="reseller_customer",
        status="active",
    )
    db.add(relationship)
    await db.flush()

    # Now scoped to the child tenant (set by provisioning above) - its own creation event,
    # the same "tenant.created.v1" shape /register itself emits.
    await record_audit_and_outbox(
        db,
        tenant_id=provisioned.tenant.id,
        actor_type="user",
        actor_id=str(context.user_id),
        action="tenant.created",
        outcome="success",
        target_type="tenant",
        target_id=str(provisioned.tenant.id),
        reason=f"Provisioned by reseller {parent_organization_id}",
        before_patch=None,
        after_patch={
            "tenant_id": str(provisioned.tenant.id),
            "organization_id": str(provisioned.organization.id),
            "parent_organization_id": str(parent_organization_id),
        },
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="tenant.created.v1",
        event_payload={
            "tenant_id": str(provisioned.tenant.id),
            "organization_id": str(provisioned.organization.id),
            "parent_organization_id": str(parent_organization_id),
        },
        aggregate_type="tenant",
        aggregate_id=str(provisioned.tenant.id),
    )

    token = await create_invitation_ticket(
        request.app.state.redis, settings,
        membership_id=provisioned.membership.id, tenant_id=provisioned.tenant.id,
        user_id=provisioned.user.id, email=body.owner_email,
    )
    invitation_link = f"{_crm_origin(settings)}/accept-invitation?token={token}"
    sent = await _send_invitation_email(
        settings, email=body.owner_email, organization_name=body.organization_name, link=invitation_link
    )

    return CreateChildTenantOut(
        tenant_id=str(provisioned.tenant.id),
        organization_id=str(provisioned.organization.id),
        display_name=body.organization_name,
        status=provisioned.tenant.status,
        owner_email=body.owner_email,
        created_at=provisioned.tenant.created_at.isoformat(),
        invitation_link=None if sent else invitation_link,
    )


def _crm_origin(settings: Settings) -> str:
    # First configured origin is the one a real person's browser actually loads (through
    # Traefik) - mirrors memberships.py's own helper.
    origins = settings.customer_crm_origins
    return origins[0] if origins else "http://app.localhost:8080"


async def _send_invitation_email(settings: Settings, *, email: str, organization_name: str, link: str) -> bool:
    registry = build_registry(settings)
    provider = registry.get("email")
    if provider is None:
        return False
    result = await provider.send(
        Message(
            recipient=email,
            subject="Your AIRIVU CSense account is ready",
            body=(
                f"A CSense organization, '{organization_name}', has been set up for you.\n\n"
                f"Set your password and sign in here: {link}\n\n"
                "This link is valid for 7 days and can only be used once."
            ),
        )
    )
    return result.accepted
