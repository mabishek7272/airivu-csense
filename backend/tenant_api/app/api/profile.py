"""The mobile Settings -> Profile screen's own data source - knowing your own name/email
isn't privileged relative to what a signed-in member already sees (branding.py's
/effective endpoint already states the same reasoning for org branding), so this needs no
extra permission check beyond a valid session.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import NotFoundError
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/profile", tags=["tenant-profile"])


class ProfileOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    tenant_id: str
    organization_display_name: str


@router.get("", response_model=ProfileOut)
async def get_profile(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ProfileOut:
    row = (
        await db.execute(
            text(
                # Correlated through memberships (not just two independently-bound-param
                # joins on users/tenants) so a user_id/tenant_id pair that doesn't actually
                # share an active membership returns 0 rows - a clean 404 - instead of
                # silently joining unrelated rows. users/tenants/organizations carry no RLS
                # (only memberships/membership_resource_scopes/audit_events do, per
                # migration 0001), so this correlation is the only thing enforcing it.
                "SELECT u.email_display, u.display_name, u.locale, u.timezone, "
                "       o.display_name AS organization_display_name "
                "FROM memberships m "
                "JOIN users u ON u.id = m.user_id "
                "JOIN tenants t ON t.id = m.tenant_id "
                "JOIN organizations o ON o.id = t.organization_id "
                "WHERE m.user_id = :user_id AND m.tenant_id = :tenant_id AND m.status = 'active'"
            ),
            {"tenant_id": context.tenant_id, "user_id": context.user_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No active membership found for this account.")
    return ProfileOut(
        user_id=str(context.user_id), email=row.email_display, display_name=row.display_name,
        locale=row.locale, timezone=row.timezone, tenant_id=str(context.tenant_id),
        organization_display_name=row.organization_display_name,
    )
