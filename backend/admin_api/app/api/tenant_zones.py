"""Admin-side mirror of `tenant_api/app/api/zones.py` — see `tenant_sites.py`'s module
docstring for why this exists and how it differs from the tenant-facing original.

**Every query here adds an explicit `tenant_id` filter that the original doesn't need.**
`zones.py`'s own queries rely on RLS alone (`db_session_for_tenant` sets `app.tenant_id`,
and the policy's tenant match does the scoping) — a `platform_session` sets no such
tenant_id, so its RLS bypass branch has no tenant restriction at all. Without an explicit
`tenant_id = :tenant_id` on every statement here, an admin managing one tenant could read
or edit another tenant's zones by ID. This applies to every file in this admin-mirror set,
not just this one.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}/zones", tags=["admin-tenant-zones"])

MIN_POINTS = 3
MAX_POINTS = 60
MIN_AREA = 0.0005

ZONE_TYPES = ("general", "restricted", "safety", "exclusion", "counting")
PRIVACY_LEVELS = ("standard", "sensitive", "high")


def polygon_area(points: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


class ZoneIn(BaseModel):
    site_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    zone_type: str = Field(default="restricted", pattern="^(" + "|".join(ZONE_TYPES) + ")$")
    privacy_level: str = Field(default="standard", pattern="^(" + "|".join(PRIVACY_LEVELS) + ")$")
    polygon: list[list[float]] = Field(min_length=MIN_POINTS, max_length=MAX_POINTS)

    @field_validator("polygon")
    @classmethod
    def _valid_polygon(cls, points: list[list[float]]) -> list[list[float]]:
        for point in points:
            if len(point) != 2:
                raise ValueError("Each point must be a pair of numbers, [x, y].")
            x, y = point
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(
                    "Points must be normalised to 0..1 against the frame, so the zone "
                    "means the same thing at any stream resolution."
                )
        area = polygon_area(points)
        if area < MIN_AREA:
            raise ValueError(
                "This shape encloses almost no area, so nothing would ever overlap it "
                "enough to trigger a rule - the rule would be silently dead. Check the "
                "points are not in a straight line."
            )
        return points


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    zone_type: str | None = Field(default=None, pattern="^(" + "|".join(ZONE_TYPES) + ")$")
    privacy_level: str | None = Field(default=None, pattern="^(" + "|".join(PRIVACY_LEVELS) + ")$")
    polygon: list[list[float]] | None = Field(default=None, min_length=MIN_POINTS, max_length=MAX_POINTS)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")

    _check_polygon = field_validator("polygon")(ZoneIn._valid_polygon.__func__)


class ZoneOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    site_name: str | None = None
    name: str
    zone_type: str
    privacy_level: str
    polygon: list[list[float]] = []
    status: str
    area_fraction: float = 0.0
    rule_count: int = 0
    created_at: dt.datetime


_SELECT = """
    SELECT z.id, z.site_id, s.name, z.name, z.zone_type, z.privacy_level,
           z.geometry_json, z.status, z.created_at,
           (SELECT count(*) FROM detection_rules r
             WHERE r.zone_id = z.id AND r.status = 'active')
    FROM zones z
    LEFT JOIN sites s ON s.id = z.site_id
"""


def _points(geometry) -> list[list[float]]:
    if not isinstance(geometry, dict):
        return []
    raw = geometry.get("polygon")
    if not isinstance(raw, list):
        return []
    out: list[list[float]] = []
    for point in raw:
        try:
            out.append([float(point[0]), float(point[1])])
        except (TypeError, IndexError, ValueError):
            return out
    return out


def _to_zone(row) -> ZoneOut:
    points = _points(row[6])
    return ZoneOut(
        id=row[0], site_id=row[1], site_name=row[2], name=row[3], zone_type=row[4],
        privacy_level=row[5], polygon=points, status=row[7], created_at=row[8],
        area_fraction=round(polygon_area(points), 5) if len(points) >= 3 else 0.0,
        rule_count=row[9],
    )


async def _load(db: AsyncSession, tenant_id: uuid.UUID, zone_id: uuid.UUID) -> ZoneOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE z.id = :id AND z.tenant_id = :tenant_id"),
            {"id": zone_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such zone.")
    return _to_zone(row)


@router.get("", response_model=list[ZoneOut])
async def list_zones(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[ZoneOut]:
    require_permission(context, "tenant.infra.manage")
    clauses = ["z.tenant_id = :tenant_id", "z.status <> 'deleted'"]
    params: dict = {"tenant_id": tenant_id, "limit": limit}
    if site_id:
        clauses.append("z.site_id = :site_id")
        params["site_id"] = site_id

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY z.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_zone(row) for row in rows]


@router.get("/{zone_id}", response_model=ZoneOut)
async def get_zone(
    tenant_id: uuid.UUID,
    zone_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ZoneOut:
    require_permission(context, "tenant.infra.manage")
    return await _load(db, tenant_id, zone_id)


@router.post("", response_model=ZoneOut, status_code=201)
async def create_zone(
    tenant_id: uuid.UUID,
    body: ZoneIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ZoneOut:
    require_permission(context, "tenant.infra.manage")

    site = (
        await db.execute(
            text("SELECT 1 FROM sites WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"),
            {"id": body.site_id, "tenant_id": tenant_id},
        )
    ).first()
    if site is None:
        raise NotFoundError("No such site.")

    zone_id = (
        await db.execute(
            text(
                """
                INSERT INTO zones
                    (tenant_id, site_id, name, zone_type, privacy_level, geometry_json)
                VALUES (:tenant_id, :site_id, :name, :zone_type, :privacy_level,
                        CAST(:geometry AS jsonb))
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "site_id": body.site_id,
                "name": body.name,
                "zone_type": body.zone_type,
                "privacy_level": body.privacy_level,
                "geometry": json.dumps({"polygon": body.polygon}),
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_zone.create",
        outcome="success",
        target_type="zone",
        target_id=str(zone_id),
        after_patch={"name": body.name, "points": len(body.polygon)},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info(
        "admin_zone_created",
        extra={"zone_id": str(zone_id), "points": len(body.polygon), "area": round(polygon_area(body.polygon), 4)},
    )
    return await _load(db, tenant_id, zone_id)


@router.patch("/{zone_id}", response_model=ZoneOut)
async def update_zone(
    tenant_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: ZonePatch,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ZoneOut:
    require_permission(context, "tenant.infra.manage")
    existing = await _load(db, tenant_id, zone_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return existing

    assignments = []
    params: dict = {"id": zone_id, "tenant_id": tenant_id}
    for field, value in changes.items():
        if field == "polygon":
            assignments.append("geometry_json = CAST(:geometry AS jsonb)")
            params["geometry"] = json.dumps({"polygon": value})
        else:
            assignments.append(f"{field} = :{field}")
            params[field] = value

    await db.execute(
        text(
            f"UPDATE zones SET {', '.join(assignments)}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND tenant_id = :tenant_id"
        ),
        params,
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_zone.update",
        outcome="success",
        target_type="zone",
        target_id=str(zone_id),
        after_patch=changes,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    if "polygon" in changes:
        logger.info(
            "admin_zone_boundary_changed",
            extra={
                "zone_id": str(zone_id), "rules_affected": existing.rule_count,
                "area_before": existing.area_fraction,
                "area_after": round(polygon_area(changes["polygon"]), 5),
            },
        )
    return await _load(db, tenant_id, zone_id)


@router.delete("/{zone_id}", status_code=204)
async def delete_zone(
    tenant_id: uuid.UUID,
    zone_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    zone = await _load(db, tenant_id, zone_id)

    if zone.rule_count:
        raise ApiError(
            status_code=409,
            code="zone_in_use",
            message=(
                f"'{zone.name}' is used by {zone.rule_count} active rule"
                f"{'s' if zone.rule_count != 1 else ''}. Removing it would widen "
                "them to the whole frame rather than disabling them, so they must be "
                "changed or disabled first."
            ),
            details={"rule_count": zone.rule_count},
        )

    await db.execute(
        text("UPDATE zones SET status = 'deleted', updated_at = now() WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": zone_id, "tenant_id": tenant_id},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_zone.delete",
        outcome="success",
        target_type="zone",
        target_id=str(zone_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_zone_deleted", extra={"zone_id": str(zone_id)})
