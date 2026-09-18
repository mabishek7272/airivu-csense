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

import datetime as dt

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.security.permissions import require_permission
from csense_shared.security.site_scope import site_scope_sql_filter
from csense_shared.security.tenant_context import TenantContext
from csense_shared.storage.objects import create_presign_client

router = APIRouter(prefix="/api/v1/tenant/dashboard", tags=["dashboard"])

# Mirrors incidents.py's own convention for what counts as "still open" - everything
# short of the two terminal states.
OPEN_INCIDENT_STATUSES = ("open", "acknowledged", "investigating", "escalated")

MAX_CAMERA_TILES = 24
# Same short TTL as incidents.py's own thumbnail URLs - this is a refetched dashboard
# widget, not a link anyone is expected to keep.
THUMBNAIL_URL_TTL = dt.timedelta(minutes=10)


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


class CameraTileOut(BaseModel):
    camera_id: str
    camera_name: str
    camera_status: str
    # None means this camera has never produced a detection snapshot yet - a real state
    # for a newly added camera, not an error. The dashboard renders that as a placeholder
    # tile rather than omitting the camera, so a quiet camera is still visible as quiet.
    thumbnail_url: str | None
    captured_at: dt.datetime | None


class CameraWallOut(BaseModel):
    tiles: list[CameraTileOut]


@router.get("/camera-thumbnails", response_model=CameraWallOut)
async def get_camera_thumbnails(
    request: Request,
    limit: int = Query(default=12, ge=1, le=MAX_CAMERA_TILES),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraWallOut:
    """Live camera wall: each camera's most recent evidence snapshot, one row per camera.

    Built on the same `evidence` table incidents.py's own thumbnails use - this is real
    data (an actual captured frame), not a placeholder or a simulated stream. A camera with
    no evidence yet (no detection has fired) still gets a tile, with `thumbnail_url: null`,
    so the wall shows every camera rather than silently dropping quiet ones.
    """
    require_permission(context, "dashboard.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="c.site_id")
    rows = (
        await db.execute(
            text(
                f"""
                SELECT c.id, c.name, c.status::text, so.bucket, so.object_key, latest.capture_time
                FROM cameras c
                LEFT JOIN LATERAL (
                    SELECT e.object_id, e.capture_time
                    FROM evidence e
                    WHERE e.camera_id = c.id
                      AND e.privacy_variant::text IN ('masked', 'annotated')
                    ORDER BY CASE e.privacy_variant::text WHEN 'masked' THEN 0 ELSE 1 END,
                             e.capture_time DESC
                    LIMIT 1
                ) latest ON true
                LEFT JOIN stored_objects so ON so.id = latest.object_id
                WHERE c.deleted_at IS NULL AND {scope_clause}
                ORDER BY latest.capture_time DESC NULLS LAST, c.name
                LIMIT :limit
                """
            ),
            {"limit": limit, **scope_params},
        )
    ).all()

    minio = create_presign_client(request.app.state.settings)
    tiles: list[CameraTileOut] = []
    for camera_id, name, status, bucket, object_key, capture_time in rows:
        thumbnail_url = None
        if bucket and object_key:
            try:
                thumbnail_url = minio.presigned_get_object(
                    bucket, object_key, expires=THUMBNAIL_URL_TTL
                )
            except Exception:
                # Same "missing object should not blank the tile" shape as incidents.py.
                thumbnail_url = None
        tiles.append(
            CameraTileOut(
                camera_id=str(camera_id),
                camera_name=name,
                camera_status=status,
                thumbnail_url=thumbnail_url,
                captured_at=capture_time,
            )
        )
    return CameraWallOut(tiles=tiles)
