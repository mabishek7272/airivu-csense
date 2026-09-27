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
                "SELECT u.email_display, u.display_name, u.locale, u.timezone, "
                "       o.display_name AS organization_display_name "
                "FROM users u "
                "JOIN tenants t ON t.id = :tenant_id "
                "JOIN organizations o ON o.id = t.organization_id "
                "WHERE u.id = :user_id"
            ),
            {"tenant_id": context.tenant_id, "user_id": context.user_id},
        )
    ).first()
    return ProfileOut(
        user_id=str(context.user_id), email=row.email_display, display_name=row.display_name,
        locale=row.locale, timezone=row.timezone, tenant_id=str(context.tenant_id),
        organization_display_name=row.organization_display_name,
    )
