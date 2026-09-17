"""Zones — the polygons that decide where a rule applies.

A zone is not decoration. `roi_overlap_fraction` measures how much of a detected object
falls inside this polygon, and a rule fires or does not on that number. Widening a zone by
a few percent can turn a busy walkway into a source of constant alerts; narrowing it can
make an intrusion stop being detected entirely. Neither produces an error.

So the geometry is validated properly rather than stored as whatever the client sent:

  **Normalised coordinates, always.** 0..1 against the frame, so the same zone means the
  same thing whether the camera streams 704x576 or 2592x1520 — and it survives someone
  switching the camera to its substream, which would otherwise silently move every
  boundary.

  **At least three points, and a real area.** Three collinear points have zero area, so
  every overlap test against them returns zero and the rule silently never fires. That is
  indistinguishable from a broken model, and much harder to find.

  **A bounded number of points.** A polygon is clipped against every detection box on
  every frame; an unbounded vertex count is a performance problem shaped like a
  configuration field.
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

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.site_scope import site_scope_sql_filter
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/zones", tags=["zones"])

MIN_POINTS = 3
# Generous for any real boundary, and low enough that clipping stays cheap on every frame.
MAX_POINTS = 60
# Below this a polygon covers so little of the frame that it is almost certainly a
# mis-click rather than an intent - and it would never overlap a detection box enough to
# fire, so the rule would be silently dead.
MIN_AREA = 0.0005

ZONE_TYPES = ("general", "restricted", "safety", "exclusion", "counting")
PRIVACY_LEVELS = ("standard", "sensitive", "high")


def polygon_area(points: list[list[float]]) -> float:
    """Shoelace area. Absolute, so vertex winding order does not change the answer."""
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
    privacy_level: str = Field(
        default="standard", pattern="^(" + "|".join(PRIVACY_LEVELS) + ")$"
    )
    # [[x, y], ...] normalised 0..1 against the frame.
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
    privacy_level: str | None = Field(
        default=None, pattern="^(" + "|".join(PRIVACY_LEVELS) + ")$"
    )
    polygon: list[list[float]] | None = Field(
        default=None, min_length=MIN_POINTS, max_length=MAX_POINTS
    )
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
    # Fraction of the frame the polygon covers. Surfaced because "why does this never
    # fire" is usually answered by a zone that is far smaller than its author believed.
    area_fraction: float = 0.0
    # How many rules point at this zone. Redrawing it changes every one of them, and that
    # is worth knowing before the drag rather than after.
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
            # Geometry predates this validation or was written directly. Report what can
            # be read rather than failing the whole listing on one bad row.
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


async def _load(db: AsyncSession, zone_id: uuid.UUID) -> ZoneOut:
    row = (
        await db.execute(text(_SELECT + " WHERE z.id = :id"), {"id": zone_id})
    ).first()
    if row is None:
        raise NotFoundError("No such zone.")
    return _to_zone(row)


@router.get("", response_model=list[ZoneOut])
async def list_zones(
    site_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ZoneOut]:
    require_permission(context, "zone.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="z.site_id")
    clauses = ["z.status <> 'deleted'", scope_clause]
    params: dict = {"limit": limit, **scope_params}
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
    zone_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ZoneOut:
    require_permission(context, "zone.read")
    return await _load(db, zone_id)


@router.post("", response_model=ZoneOut, status_code=201)
async def create_zone(
    body: ZoneIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ZoneOut:
    require_permission(context, "zone.manage")

    site = (
        await db.execute(text("SELECT 1 FROM sites WHERE id = :id"), {"id": body.site_id})
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
                "tenant_id": context.tenant_id,
                "site_id": body.site_id,
                "name": body.name,
                "zone_type": body.zone_type,
                "privacy_level": body.privacy_level,
                "geometry": json.dumps({"polygon": body.polygon}),
            },
        )
    ).scalar_one()

    logger.info(
        "zone_created",
        extra={"zone_id": str(zone_id), "points": len(body.polygon),
               "area": round(polygon_area(body.polygon), 4)},
    )
    return await _load(db, zone_id)


@router.patch("/{zone_id}", response_model=ZoneOut)
async def update_zone(
    zone_id: uuid.UUID,
    body: ZonePatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ZoneOut:
    require_permission(context, "zone.manage")
    existing = await _load(db, zone_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return existing

    assignments = []
    params: dict = {"id": zone_id}
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
            "version = version + 1 WHERE id = :id"
        ),
        params,
    )

    if "polygon" in changes:
        # Its own line, with the rule count: redrawing a boundary changes what every rule
        # pointing at it will and will not fire on, and "why did alerts change on Tuesday"
        # needs to be answerable afterwards.
        logger.info(
            "zone_boundary_changed",
            extra={
                "zone_id": str(zone_id),
                "rules_affected": existing.rule_count,
                "area_before": existing.area_fraction,
                "area_after": round(polygon_area(changes["polygon"]), 5),
            },
        )
    return await _load(db, zone_id)


@router.delete("/{zone_id}", status_code=204)
async def delete_zone(
    zone_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Removes a zone, provided no active rule depends on it.

    Refused rather than cascaded, and for a sharper reason than with sites: deleting a
    zone out from under a rule does not disable that rule. The foreign key sets its
    zone_id to NULL, and a rule with no zone watches the *whole frame* - so tidying away
    an unused-looking zone can silently turn a tightly-scoped rule into one that fires on
    everything the camera can see.
    """
    require_permission(context, "zone.manage")
    zone = await _load(db, zone_id)

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
        text("UPDATE zones SET status = 'deleted', updated_at = now() WHERE id = :id"),
        {"id": zone_id},
    )
    logger.info("zone_deleted", extra={"zone_id": str(zone_id)})
