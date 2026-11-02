"""Admin-side mirror of `tenant_api/app/api/sites.py` — sites are now managed by AIRIVU
staff on a tenant's behalf, not by the tenant themselves (see CHECKLIST.md's "move
camera/site/zone/edge/rules/pipeline management to the Console" entry). Same queries,
same validation; the only real difference is where tenant scope comes from: a JWT-derived
`context.tenant_id` there, an explicit `tenant_id` path segment here, because a platform
session has no "current tenant" of its own. Every write is audited with
`actor_type="platform_developer"` so "AIRIVU added this site for Apti" is a real,
inspectable record — tenant_api's own sites.py has no equivalent audit call today because
a tenant acting on their own data doesn't need one; a platform operator acting on someone
else's does.
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
from csense_shared.timezones import OFFERED_TIMEZONES, is_usable_timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}/sites", tags=["admin-tenant-sites"])


class Address(BaseModel):
    line1: str | None = Field(default=None, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=120)


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    timezone: str = Field(default="UTC", max_length=64)
    address: Address | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if not is_usable_timezone(value):
            raise ValueError(
                f"'{value}' is not a timezone this server can resolve. Use an IANA name "
                "like 'Asia/Kolkata' or 'Australia/Sydney' — rule schedules are evaluated "
                "in this zone, so a wrong one makes overnight rules fire during the day."
            )
        return value


class SitePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    address: Address | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")

    _check_zone = field_validator("timezone")(SiteIn._known_zone.__func__)


class SiteOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    timezone: str
    address: dict | None = None
    latitude: float | None = None
    longitude: float | None = None
    status: str
    camera_count: int = 0
    device_count: int = 0
    created_at: dt.datetime


_SELECT = """
    SELECT s.id, s.name, s.code, s.timezone, s.address_json, s.latitude, s.longitude,
           s.status, s.created_at,
           (SELECT count(*) FROM cameras c
             WHERE c.site_id = s.id AND c.deleted_at IS NULL),
           (SELECT count(*) FROM edge_devices d
             WHERE d.site_id = s.id AND d.deleted_at IS NULL)
    FROM sites s
"""


def _to_site(row) -> SiteOut:
    return SiteOut(
        id=row[0], name=row[1], code=row[2], timezone=row[3], address=row[4],
        latitude=float(row[5]) if row[5] is not None else None,
        longitude=float(row[6]) if row[6] is not None else None,
        status=row[7], created_at=row[8], camera_count=row[9], device_count=row[10],
    )


async def _load(db: AsyncSession, tenant_id: uuid.UUID, site_id: uuid.UUID) -> SiteOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE s.id = :id AND s.tenant_id = :tenant_id AND s.deleted_at IS NULL"),
            {"id": site_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such site.")
    return _to_site(row)


@router.get("", response_model=list[SiteOut])
async def list_sites(
    tenant_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[SiteOut]:
    require_permission(context, "tenant.infra.manage")
    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE s.tenant_id = :tenant_id AND s.deleted_at IS NULL ORDER BY s.name LIMIT :limit"),
            {"tenant_id": tenant_id, "limit": limit},
        )
    ).all()
    return [_to_site(row) for row in rows]


@router.get("/{site_id}", response_model=SiteOut)
async def get_site(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SiteOut:
    require_permission(context, "tenant.infra.manage")
    return await _load(db, tenant_id, site_id)


@router.post("", response_model=SiteOut, status_code=201)
async def create_site(
    tenant_id: uuid.UUID,
    body: SiteIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SiteOut:
    require_permission(context, "tenant.infra.manage")

    clash = (
        await db.execute(
            text("SELECT 1 FROM sites WHERE tenant_id = :tenant_id AND code = :code AND deleted_at IS NULL"),
            {"tenant_id": tenant_id, "code": body.code},
        )
    ).first()
    if clash:
        raise ApiError(
            status_code=409,
            code="site_code_taken",
            message=f"A site with code '{body.code}' already exists for this tenant.",
        )

    site_id = (
        await db.execute(
            text(
                """
                INSERT INTO sites
                    (tenant_id, name, code, timezone, address_json, latitude, longitude)
                VALUES (:tenant_id, :name, :code, :timezone,
                        CAST(:address AS jsonb), :latitude, :longitude)
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "name": body.name,
                "code": body.code,
                "timezone": body.timezone,
                "address": json.dumps(body.address.model_dump(exclude_none=True))
                if body.address
                else None,
                "latitude": body.latitude,
                "longitude": body.longitude,
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_site.create",
        outcome="success",
        target_type="site",
        target_id=str(site_id),
        after_patch={"name": body.name, "code": body.code},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_site_created", extra={"site_id": str(site_id), "tenant_id": str(tenant_id)})
    return await _load(db, tenant_id, site_id)


@router.patch("/{site_id}", response_model=SiteOut)
async def update_site(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID,
    body: SitePatch,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SiteOut:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, site_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await _load(db, tenant_id, site_id)

    assignments = []
    params: dict = {"id": site_id, "tenant_id": tenant_id}
    for field, value in changes.items():
        if field == "address":
            assignments.append("address_json = CAST(:address AS jsonb)")
            params["address"] = (
                json.dumps(Address(**value).model_dump(exclude_none=True)) if value else None
            )
        else:
            assignments.append(f"{field} = :{field}")
            params[field] = value

    await db.execute(
        text(
            f"UPDATE sites SET {', '.join(assignments)}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
        ),
        params,
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_site.update",
        outcome="success",
        target_type="site",
        target_id=str(site_id),
        after_patch=changes,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_site_updated", extra={"site_id": str(site_id), "fields": sorted(changes)})
    return await _load(db, tenant_id, site_id)


@router.delete("/{site_id}", status_code=204)
async def delete_site(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    site = await _load(db, tenant_id, site_id)

    if site.camera_count or site.device_count:
        parts = []
        if site.camera_count:
            parts.append(f"{site.camera_count} camera{'s' if site.camera_count != 1 else ''}")
        if site.device_count:
            parts.append(f"{site.device_count} edge device{'s' if site.device_count != 1 else ''}")
        raise ApiError(
            status_code=409,
            code="site_not_empty",
            message=(
                f"'{site.name}' still has {' and '.join(parts)} attached. Move or remove "
                "them first — deleting the site would take them with it."
            ),
            details={"camera_count": site.camera_count, "device_count": site.device_count},
        )

    await db.execute(
        text(
            "UPDATE sites SET deleted_at = now(), status = 'inactive', updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": site_id, "tenant_id": tenant_id},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_site.delete",
        outcome="success",
        target_type="site",
        target_id=str(site_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_site_deleted", extra={"site_id": str(site_id)})


@router.get("/meta/timezones", response_model=list[str])
async def list_timezones(
    tenant_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
) -> list[str]:
    require_permission(context, "tenant.infra.manage")
    return OFFERED_TIMEZONES
