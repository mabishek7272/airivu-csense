"""Sites — the physical places cameras and edge devices belong to.

A site is mostly bookkeeping, with one field that is not: **timezone**. Rule schedules are
written by people thinking in local time — "nobody in the yard after 22:00" means 22:00 at
the gate — and the pipeline converts using this value before evaluating. A wrong timezone
does not produce an error anywhere; it produces an overnight rule that fires during the
working day and stays silent at night. So it is validated against the real IANA database
here rather than accepted as free text and discovered later.

**Deleting a site with cameras is refused, not cascaded.** A cascade would silently remove
the cameras and everything hanging off them because someone tidied a list. The error names
how many are attached and what to do, which turns a destructive surprise into a decision.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
import zoneinfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/sites", tags=["sites"])

# Read once. The set is a few hundred strings and the lookup happens on every write.
_VALID_TIMEZONES = zoneinfo.available_timezones()


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
        if value not in _VALID_TIMEZONES:
            raise ValueError(
                f"'{value}' is not a known IANA timezone. Use a name like "
                "'Asia/Kolkata' or 'Australia/Sydney' — rule schedules are evaluated in "
                "this zone, so a wrong one makes overnight rules fire during the day."
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
    # Shown in the listing so the consequence of removing a site is visible before
    # anyone tries.
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


async def _load(db: AsyncSession, site_id: uuid.UUID) -> SiteOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE s.id = :id AND s.deleted_at IS NULL"), {"id": site_id}
        )
    ).first()
    if row is None:
        raise NotFoundError("No such site.")
    return _to_site(row)


@router.get("", response_model=list[SiteOut])
async def list_sites(
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[SiteOut]:
    require_permission(context, "site.read")
    rows = (
        await db.execute(
            text(_SELECT + " WHERE s.deleted_at IS NULL ORDER BY s.name LIMIT :limit"),
            {"limit": limit},
        )
    ).all()
    return [_to_site(row) for row in rows]


@router.get("/{site_id}", response_model=SiteOut)
async def get_site(
    site_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SiteOut:
    require_permission(context, "site.read")
    return await _load(db, site_id)


@router.post("", response_model=SiteOut, status_code=201)
async def create_site(
    body: SiteIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SiteOut:
    require_permission(context, "site.manage")

    clash = (
        await db.execute(
            text("SELECT 1 FROM sites WHERE code = :code AND deleted_at IS NULL"),
            {"code": body.code},
        )
    ).first()
    if clash:
        raise ApiError(
            status_code=409,
            code="site_code_taken",
            message=f"A site with code '{body.code}' already exists.",
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
                "tenant_id": context.tenant_id,
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

    logger.info(
        "site_created",
        extra={"site_id": str(site_id), "tenant_id": str(context.tenant_id)},
    )
    return await _load(db, site_id)


@router.patch("/{site_id}", response_model=SiteOut)
async def update_site(
    site_id: uuid.UUID,
    body: SitePatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SiteOut:
    require_permission(context, "site.manage")
    await _load(db, site_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await _load(db, site_id)

    assignments = []
    params: dict = {"id": site_id}
    for field, value in changes.items():
        if field == "address":
            assignments.append("address_json = CAST(:address AS jsonb)")
            params["address"] = (
                json.dumps(Address(**value).model_dump(exclude_none=True))
                if value
                else None
            )
        else:
            assignments.append(f"{field} = :{field}")
            params[field] = value

    await db.execute(
        text(
            f"UPDATE sites SET {', '.join(assignments)}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND deleted_at IS NULL"
        ),
        params,
    )
    if "timezone" in changes:
        # Worth its own line: changing a site's zone silently re-times every schedule
        # attached to it, and that is the kind of change someone needs to be able to find
        # afterwards when an alert arrives at an unexpected hour.
        logger.info(
            "site_timezone_changed",
            extra={"site_id": str(site_id), "timezone": changes["timezone"]},
        )
    logger.info("site_updated", extra={"site_id": str(site_id), "fields": sorted(changes)})
    return await _load(db, site_id)


@router.delete("/{site_id}", status_code=204)
async def delete_site(
    site_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Removes a site, provided nothing is still attached to it.

    Refuses rather than cascading. A cascade here would delete every camera at the site
    and everything hanging off them, because someone tidied a list — and the person
    clicking would have no way to know that was about to happen. Naming the count turns a
    destructive surprise into a decision.
    """
    require_permission(context, "site.manage")
    site = await _load(db, site_id)

    if site.camera_count or site.device_count:
        parts = []
        if site.camera_count:
            parts.append(f"{site.camera_count} camera{'s' if site.camera_count != 1 else ''}")
        if site.device_count:
            parts.append(
                f"{site.device_count} edge device{'s' if site.device_count != 1 else ''}"
            )
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
            "WHERE id = :id"
        ),
        {"id": site_id},
    )
    logger.info("site_deleted", extra={"site_id": str(site_id)})


@router.get("/meta/timezones", response_model=list[str])
async def list_timezones(
    context: TenantContext = Depends(current_tenant_context),
) -> list[str]:
    """The zones a site may be set to.

    Served from the server's own IANA database rather than hardcoded in the client, so the
    list the UI offers is exactly the list the validator accepts. A picker that can produce
    a value the API rejects is worse than a free-text field.
    """
    require_permission(context, "site.read")
    return sorted(_VALID_TIMEZONES)
