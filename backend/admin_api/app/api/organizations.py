"""Platform organization listing (docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §10.2:
`POST /api/v1/admin/organizations`; this adds the corresponding read to prove the
platform-privileged, cross-tenant repository path end-to-end). Requires
`organization.manage` — there is no cross-tenant admin read without an explicit
permission (TRD §7.2/§7.3).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.db.models import Organization
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

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
