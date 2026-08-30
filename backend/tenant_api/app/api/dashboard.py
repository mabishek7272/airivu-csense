"""A tenant's own dashboard summary (TRD §10.2's own representative endpoint,
`GET /api/v1/tenant/dashboard`, previously unbuilt). Read-only counts across resources
this tenant already has individual read permissions for - `dashboard.read` (migration
0040) is one permission for one aggregate view, not a new access grant.

A freshly created tenant (self-registered, or a reseller's own child tenant the moment its
owner accepts the invitation) has every count at zero - this is the "empty dashboard" step
of the Phase 2 vertical-slice test (`scripts/e2e_vertical_slice.py`), and it must render
as a real, correct zero-state, not an error or a missing field.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/dashboard", tags=["dashboard"])

# Mirrors incidents.py's own convention for what counts as "still open" - everything
# short of the two terminal states.
OPEN_INCIDENT_STATUSES = ("open", "acknowledged", "investigating", "escalated")


class DashboardOut(BaseModel):
    sites_count: int
    cameras_count: int
    incidents_open_count: int
    incidents_total_count: int
    team_members_count: int


@router.get("", response_model=DashboardOut)
async def get_dashboard(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DashboardOut:
    require_permission(context, "dashboard.read")

    sites_count = (
        await db.execute(text("SELECT count(*) FROM sites WHERE deleted_at IS NULL"))
    ).scalar_one()
    cameras_count = (
        await db.execute(text("SELECT count(*) FROM cameras WHERE deleted_at IS NULL"))
    ).scalar_one()
    incidents_open_count = (
        await db.execute(
            text("SELECT count(*) FROM incidents WHERE status::text = ANY(:statuses)"),
            {"statuses": list(OPEN_INCIDENT_STATUSES)},
        )
    ).scalar_one()
    incidents_total_count = (
        await db.execute(text("SELECT count(*) FROM incidents"))
    ).scalar_one()
    team_members_count = (
        await db.execute(text("SELECT count(*) FROM memberships WHERE status = 'active'"))
    ).scalar_one()

    return DashboardOut(
        sites_count=sites_count,
        cameras_count=cameras_count,
        incidents_open_count=incidents_open_count,
        incidents_total_count=incidents_total_count,
        team_members_count=team_members_count,
    )
