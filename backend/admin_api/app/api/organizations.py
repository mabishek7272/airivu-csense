"""Platform organization listing and creation (docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md
§10.2: `POST /api/v1/admin/organizations`). Requires `organization.manage` — there is no
cross-tenant admin read or write without an explicit permission (TRD §7.2/§7.3).

Creation provisions a brand-new organization + tenant with an *invited* owner
(`csense_shared.tenancy.provisioning`) - a platform admin never learns the owner's chosen
password, so the owner activates through the existing, unmodified
`POST /api/v1/auth/accept-invitation`, exactly like a Tenant API membership invite does.
This is how a `reseller` organization comes into being in the first place (§8.2: "A
reseller is an organization with reseller entitlements") - a `direct_customer` org can also
be created this way for a platform-assisted onboarding, as an alternative to open
self-registration (`POST /api/v1/auth/register`). `reseller_customer` is deliberately not
an allowed value here - those are only ever created by a reseller itself, through
`POST /api/v1/tenant/child-tenants`, which is what records the `organization_relationships`
row linking parent to child.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, get_app_settings, platform_db_session
from app.repositories.identity import get_role_by_name
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.db.models import Organization
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.invitation_tickets import create_invitation_ticket
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext
from csense_shared.tenancy import provision_organization_with_invited_owner

router = APIRouter(prefix="/api/v1/admin/organizations", tags=["admin-organizations"])


class OrganizationOut(BaseModel):
    id: str
    display_name: str
    organization_type: str
    status: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[OrganizationOut])
async def list_organizations(
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[OrganizationOut]:
    require_permission(context, "organization.manage")
    result = await db.execute(select(Organization).order_by(Organization.created_at.desc()).limit(100))
    return [
        OrganizationOut(
            id=str(org.id), display_name=org.display_name, organization_type=org.organization_type, status=org.status
        )
        for org in result.scalars().all()
    ]


class CreateOrganizationIn(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    organization_type: str = Field(pattern="^(direct_customer|reseller)$")
    owner_email: EmailStr
    owner_display_name: str = Field(min_length=1, max_length=200)


class CreateOrganizationOut(BaseModel):
    organization_id: str
    tenant_id: str
    organization_type: str
    display_name: str
    owner_email: str
    # Only set when the invitation email could not actually be sent - see
    # memberships.py's invite_member for the same reasoning applied here.
    invitation_link: str | None = None


@router.post("", response_model=CreateOrganizationOut, status_code=201)
async def create_organization(
    body: CreateOrganizationIn,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> CreateOrganizationOut:
    require_permission(context, "organization.manage")

    owner_role = await get_role_by_name(db, "tenant_owner", "customer")

    provisioned = await provision_organization_with_invited_owner(
        db,
        organization_type=body.organization_type,
        organization_name=body.organization_name,
        owner_email=body.owner_email,
        owner_display_name=body.owner_display_name,
        owner_role=owner_role,
    )

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="organization.create",
        outcome="success",
        target_type="organization",
        target_id=str(provisioned.organization.id),
        reason=f"Created {body.organization_type} organization '{body.organization_name}'",
        before_patch=None,
        after_patch={
            "organization_type": body.organization_type,
            "tenant_id": str(provisioned.tenant.id),
        },
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="organization.created.v1",
        event_payload={
            "organization_id": str(provisioned.organization.id),
            "tenant_id": str(provisioned.tenant.id),
            "organization_type": body.organization_type,
        },
        aggregate_type="organization",
        aggregate_id=str(provisioned.organization.id),
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

    return CreateOrganizationOut(
        organization_id=str(provisioned.organization.id),
        tenant_id=str(provisioned.tenant.id),
        organization_type=body.organization_type,
        display_name=body.organization_name,
        owner_email=body.owner_email,
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
            subject="Your AIRIVU CSense organization is ready",
            body=(
                f"A CSense organization, '{organization_name}', has been provisioned for you.\n\n"
                f"Set your password and sign in here: {link}\n\n"
                "This link is valid for 7 days and can only be used once."
            ),
        )
    )
    return result.accepted
