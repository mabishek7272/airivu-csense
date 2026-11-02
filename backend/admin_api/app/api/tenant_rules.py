"""Admin-side mirror of `tenant_api/app/api/rules.py` — see `tenant_sites.py`'s module
docstring for why this exists and its warning about explicit `tenant_id` filtering (a
`platform_session` sets no tenant scope for RLS to filter by).

Same rules as the original, including the night-confidence warning — the underlying camera
this session diagnosed (`apti-demo-cam1`) is exactly the case this warning exists for: a
0.5 threshold detects nothing at night on an infrared camera.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}/rules", tags=["admin-tenant-rules"])

SEVERITIES = ("low", "medium", "high", "critical")
TYPE_CODES = (
    "zone.intrusion", "ppe.violation", "fire.smoke", "loitering",
    "crowd.density", "vehicle.unauthorised", "fall.detected",
    "face.unknown", "face.watchlist",
)
NIGHT_RISK_CONFIDENCE = 0.45


class RuleIn(BaseModel):
    site_id: uuid.UUID
    camera_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    type_code: str = Field(default="zone.intrusion", min_length=3, max_length=64)
    alertable_classes: list[str] = Field(min_length=1, max_length=20)
    min_confidence: float = Field(default=0.4, gt=0, le=1)
    severity: str = Field(default="high", pattern="^(" + "|".join(SEVERITIES) + ")$")
    min_roi_overlap: float = Field(default=0.3, ge=0, le=1)
    min_consecutive_frames: int = Field(default=1, ge=1, le=100)
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    active_from_hour: int | None = Field(default=None, ge=0, le=23)
    active_to_hour: int | None = Field(default=None, ge=0, le=23)

    @field_validator("alertable_classes")
    @classmethod
    def _clean_classes(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip().lower() for v in values if v and v.strip()]
        if not cleaned:
            raise ValueError("Choose at least one object class. A rule with none can never match.")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _hours_are_paired(self):
        if (self.active_from_hour is None) != (self.active_to_hour is None):
            raise ValueError(
                "Set both a start and an end hour, or neither. One half of a window has "
                "no meaning, and the evaluator would have to guess the other."
            )
        if self.active_from_hour is not None and self.active_from_hour == self.active_to_hour:
            raise ValueError(
                "A window that starts and ends at the same hour is empty, so the rule "
                "would never be active. Leave both blank for 'always'."
            )
        return self


class RulePatch(BaseModel):
    camera_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    type_code: str | None = Field(default=None, min_length=3, max_length=64)
    alertable_classes: list[str] | None = Field(default=None, min_length=1, max_length=20)
    min_confidence: float | None = Field(default=None, gt=0, le=1)
    severity: str | None = Field(default=None, pattern="^(" + "|".join(SEVERITIES) + ")$")
    min_roi_overlap: float | None = Field(default=None, ge=0, le=1)
    min_consecutive_frames: int | None = Field(default=None, ge=1, le=100)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)
    active_from_hour: int | None = Field(default=None, ge=0, le=23)
    active_to_hour: int | None = Field(default=None, ge=0, le=23)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")

    _clean = field_validator("alertable_classes")(RuleIn._clean_classes.__func__)


class RuleOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    site_name: str | None = None
    camera_id: uuid.UUID | None = None
    camera_name: str | None = None
    zone_id: uuid.UUID | None = None
    zone_name: str | None = None
    name: str
    type_code: str
    alertable_classes: list[str] = []
    min_confidence: float
    severity: str
    min_roi_overlap: float
    min_consecutive_frames: int
    cooldown_seconds: int
    active_from_hour: int | None = None
    active_to_hour: int | None = None
    site_timezone: str | None = None
    status: str
    warnings: list[str] = []
    created_at: dt.datetime


_SELECT = """
    SELECT r.id, r.site_id, s.name, r.camera_id, c.name, r.zone_id, z.name, r.name,
           r.type_code, r.alertable_classes, r.min_confidence, r.severity,
           r.min_roi_overlap, r.min_consecutive_frames, r.cooldown_seconds,
           r.active_from_hour, r.active_to_hour, r.status, r.created_at, s.timezone
    FROM detection_rules r
    LEFT JOIN sites s ON s.id = r.site_id
    LEFT JOIN cameras c ON c.id = r.camera_id
    LEFT JOIN zones z ON z.id = r.zone_id
"""


def _warnings(row) -> list[str]:
    notes: list[str] = []
    confidence = float(row[10])
    zone_id, camera_id = row[5], row[3]

    if confidence > NIGHT_RISK_CONFIDENCE:
        notes.append(
            f"At {confidence:.2f}, this will likely miss detections at night. On an "
            "infrared frame a person scores around 0.1 to 0.2, so a threshold this high "
            "means the rule is effectively daytime-only."
        )
    if zone_id is None:
        notes.append("No zone, so this watches the whole frame. Anything the camera can see counts.")
    if camera_id is None:
        notes.append("No camera, so this applies to every camera at the site.")
    if int(row[14]) == 0:
        notes.append("No cooldown, so one continuing situation can raise repeated incidents.")
    if int(row[13]) > 10:
        notes.append(
            f"Requires {row[13]} consecutive frames. At a few frames per second that is "
            "several seconds of continuous presence before anything is raised."
        )
    return notes


def _to_rule(row) -> RuleOut:
    classes = row[9] if isinstance(row[9], list) else []
    return RuleOut(
        id=row[0], site_id=row[1], site_name=row[2], camera_id=row[3], camera_name=row[4],
        zone_id=row[5], zone_name=row[6], name=row[7], type_code=row[8],
        alertable_classes=[str(c) for c in classes],
        min_confidence=float(row[10]), severity=row[11], min_roi_overlap=float(row[12]),
        min_consecutive_frames=row[13], cooldown_seconds=row[14],
        active_from_hour=row[15], active_to_hour=row[16], status=row[17],
        created_at=row[18], site_timezone=row[19], warnings=_warnings(row),
    )


async def _load(db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID) -> RuleOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE r.id = :id AND r.tenant_id = :tenant_id"),
            {"id": rule_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such rule.")
    return _to_rule(row)


async def _check_references(
    db: AsyncSession, tenant_id: uuid.UUID, *,
    site_id: uuid.UUID, camera_id: uuid.UUID | None, zone_id: uuid.UUID | None,
) -> None:
    if (
        await db.execute(
            text("SELECT 1 FROM sites WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": site_id, "tenant_id": tenant_id},
        )
    ).first() is None:
        raise NotFoundError("No such site.")

    if camera_id is not None:
        row = (
            await db.execute(
                text(
                    "SELECT site_id FROM cameras WHERE id = :id AND tenant_id = :tenant_id "
                    "AND deleted_at IS NULL"
                ),
                {"id": camera_id, "tenant_id": tenant_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("No such camera.")
        if row[0] != site_id:
            raise ApiError(
                status_code=422, code="camera_not_at_site",
                message="That camera belongs to a different site. A rule is resolved through the camera's own site, so this one would never match anything.",
            )

    if zone_id is not None:
        row = (
            await db.execute(
                text(
                    "SELECT site_id FROM zones WHERE id = :id AND tenant_id = :tenant_id "
                    "AND status <> 'deleted'"
                ),
                {"id": zone_id, "tenant_id": tenant_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("No such zone.")
        if row[0] != site_id:
            raise ApiError(status_code=422, code="zone_not_at_site", message="That zone belongs to a different site.")


@router.get("", response_model=list[RuleOut])
async def list_rules(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    limit: int = Query(default=100, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[RuleOut]:
    require_permission(context, "tenant.infra.manage")
    clauses = ["r.tenant_id = :tenant_id"]
    params: dict = {"tenant_id": tenant_id, "limit": limit}
    if site_id:
        clauses.append("r.site_id = :site_id")
        params["site_id"] = site_id
    if camera_id:
        clauses.append("(r.camera_id = :camera_id OR r.camera_id IS NULL)")
        params["camera_id"] = camera_id
    if status:
        clauses.append("r.status = :status")
        params["status"] = status

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY r.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_rule(row) for row in rows]


@router.get("/{rule_id}", response_model=RuleOut)
async def get_rule(
    tenant_id: uuid.UUID,
    rule_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> RuleOut:
    require_permission(context, "tenant.infra.manage")
    return await _load(db, tenant_id, rule_id)


@router.post("", response_model=RuleOut, status_code=201)
async def create_rule(
    tenant_id: uuid.UUID,
    body: RuleIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> RuleOut:
    require_permission(context, "tenant.infra.manage")
    await _check_references(db, tenant_id, site_id=body.site_id, camera_id=body.camera_id, zone_id=body.zone_id)

    rule_id = (
        await db.execute(
            text(
                """
                INSERT INTO detection_rules
                    (tenant_id, site_id, camera_id, zone_id, name, type_code,
                     alertable_classes, min_confidence, severity, min_roi_overlap,
                     min_consecutive_frames, cooldown_seconds, active_from_hour,
                     active_to_hour)
                VALUES (:tenant_id, :site_id, :camera_id, :zone_id, :name, :type_code,
                        CAST(:classes AS jsonb), :min_confidence, :severity,
                        :min_roi_overlap, :min_consecutive_frames, :cooldown_seconds,
                        :active_from_hour, :active_to_hour)
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id, "site_id": body.site_id, "camera_id": body.camera_id,
                "zone_id": body.zone_id, "name": body.name, "type_code": body.type_code,
                "classes": json.dumps(body.alertable_classes),
                "min_confidence": body.min_confidence, "severity": body.severity,
                "min_roi_overlap": body.min_roi_overlap,
                "min_consecutive_frames": body.min_consecutive_frames,
                "cooldown_seconds": body.cooldown_seconds,
                "active_from_hour": body.active_from_hour, "active_to_hour": body.active_to_hour,
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_rule.create",
        outcome="success",
        target_type="rule",
        target_id=str(rule_id),
        after_patch={"name": body.name, "type_code": body.type_code, "min_confidence": body.min_confidence},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info(
        "admin_rule_created",
        extra={"rule_id": str(rule_id), "type_code": body.type_code, "min_confidence": body.min_confidence},
    )
    return await _load(db, tenant_id, rule_id)


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    tenant_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: RulePatch,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> RuleOut:
    require_permission(context, "tenant.infra.manage")
    existing = await _load(db, tenant_id, rule_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return existing

    await _check_references(
        db, tenant_id,
        site_id=existing.site_id,
        camera_id=changes.get("camera_id", existing.camera_id),
        zone_id=changes.get("zone_id", existing.zone_id),
    )

    assignments = []
    params: dict = {"id": rule_id, "tenant_id": tenant_id}
    for field, value in changes.items():
        if field == "alertable_classes":
            assignments.append("alertable_classes = CAST(:classes AS jsonb)")
            params["classes"] = json.dumps(value)
        else:
            assignments.append(f"{field} = :{field}")
            params[field] = value

    await db.execute(
        text(
            f"UPDATE detection_rules SET {', '.join(assignments)}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND tenant_id = :tenant_id"
        ),
        params,
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_rule.update",
        outcome="success",
        target_type="rule",
        target_id=str(rule_id),
        after_patch=changes,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_rule_updated", extra={"rule_id": str(rule_id), "fields": sorted(changes)})
    return await _load(db, tenant_id, rule_id)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    tenant_id: uuid.UUID,
    rule_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    rule = await _load(db, tenant_id, rule_id)

    await db.execute(
        text("DELETE FROM detection_rules WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": rule_id, "tenant_id": tenant_id},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_rule.delete",
        outcome="success",
        target_type="rule",
        target_id=str(rule_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_rule_deleted", extra={"rule_id": str(rule_id), "rule_name": rule.name})
