"""Detection rules — what turns a detection into an incident.

This is the last piece that had to be set up in SQL. With it, "tell me when a person
enters the loading dock after 22:00" is a form rather than an INSERT.

Every field here is a way to make a rule silently useless, which is why the validation
explains consequences rather than just ranges:

  **`min_confidence` is the sharpest edge.** A rule set to 0.5 detects nothing at night on
  an infrared camera — measured on the reference NVR, a person in darkness scores 0.09 to
  0.21. The rule looks configured, the camera looks healthy, and nobody is ever alerted.
  So a high threshold is warned about rather than silently accepted.

  **Active hours are the site's local hours**, converted before evaluation. A rule written
  as 22:00–06:00 against a site whose timezone is wrong fires through the working day and
  sleeps at night.

  **Cooldown is what stops one intruder becoming forty alerts.** Zero is allowed because a
  test deployment wants it, but it is not a sensible steady state and the API says so.

A rule with no zone watches the whole frame, and one with no camera applies to every
camera at the site. Both are legitimate and both are easy to do by accident, so both are
reported explicitly rather than shown as an empty field.
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

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.site_scope import site_scope_sql_filter
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/rules", tags=["rules"])

SEVERITIES = ("low", "medium", "high", "critical")

# The event codes the pipeline knows how to render into an alert. A code outside this set
# would produce an incident titled with its own identifier, which is ugly but not broken -
# so the list is advisory in the UI and not enforced here.
TYPE_CODES = (
    "zone.intrusion",
    "ppe.violation",
    "fire.smoke",
    "loitering",
    "crowd.density",
    "vehicle.unauthorised",
    "fall.detected",
)

# Above this, a rule will miss detections on an infrared night frame - measured at 0.09 to
# 0.21 for a person in darkness on the reference camera. Not refused, because a daytime-only
# rule may legitimately want it, but reported so the choice is deliberate.
NIGHT_RISK_CONFIDENCE = 0.45


class RuleIn(BaseModel):
    site_id: uuid.UUID
    # None means every camera at the site. "Nobody in the yard after 22:00" is written once
    # rather than once per camera.
    camera_id: uuid.UUID | None = None
    # None means the whole frame.
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
            raise ValueError(
                "Choose at least one object class. A rule with none can never match."
            )
        # Deduplicated so the same class listed twice does not read as two conditions.
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _hours_are_paired(self):
        if (self.active_from_hour is None) != (self.active_to_hour is None):
            raise ValueError(
                "Set both a start and an end hour, or neither. One half of a window has "
                "no meaning, and the evaluator would have to guess the other."
            )
        if (
            self.active_from_hour is not None
            and self.active_from_hour == self.active_to_hour
        ):
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
    # The site's zone, so the UI can say "22:00–06:00 Asia/Kolkata" rather than leaving
    # the reader to assume it is their own local time.
    site_timezone: str | None = None
    status: str
    # Plain statements about what this rule will and will not do. Derived rather than
    # stored, so they cannot go stale against the values they describe.
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
    """What this rule will quietly fail to do.

    Every one of these is a configuration that looks correct and produces no alerts, or far
    too many. Stating them where the rule is read is the only place they are cheap to
    notice - by the time an incident is missing, nobody is looking at this screen.
    """
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
        notes.append(
            "No zone, so this watches the whole frame. Anything the camera can see counts."
        )
    if camera_id is None:
        notes.append("No camera, so this applies to every camera at the site.")
    if int(row[14]) == 0:
        notes.append(
            "No cooldown, so one continuing situation can raise repeated incidents."
        )
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


async def _load(db: AsyncSession, rule_id: uuid.UUID) -> RuleOut:
    row = (
        await db.execute(text(_SELECT + " WHERE r.id = :id"), {"id": rule_id})
    ).first()
    if row is None:
        raise NotFoundError("No such rule.")
    return _to_rule(row)


async def _check_references(
    db: AsyncSession,
    *,
    site_id: uuid.UUID,
    camera_id: uuid.UUID | None,
    zone_id: uuid.UUID | None,
) -> None:
    """Confirms the site, camera and zone exist and belong together.

    Row-level security already scopes each lookup to the tenant, so this is about
    coherence: a rule pointing at a camera on a different site would never match anything,
    because ingestion resolves rules by the camera's own site.
    """
    if (
        await db.execute(text("SELECT 1 FROM sites WHERE id = :id"), {"id": site_id})
    ).first() is None:
        raise NotFoundError("No such site.")

    if camera_id is not None:
        row = (
            await db.execute(
                text("SELECT site_id FROM cameras WHERE id = :id AND deleted_at IS NULL"),
                {"id": camera_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("No such camera.")
        if row[0] != site_id:
            raise ApiError(
                status_code=422,
                code="camera_not_at_site",
                message=(
                    "That camera belongs to a different site. A rule is resolved through "
                    "the camera's own site, so this one would never match anything."
                ),
            )

    if zone_id is not None:
        row = (
            await db.execute(
                text("SELECT site_id FROM zones WHERE id = :id AND status <> 'deleted'"),
                {"id": zone_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("No such zone.")
        if row[0] != site_id:
            raise ApiError(
                status_code=422,
                code="zone_not_at_site",
                message="That zone belongs to a different site.",
            )


@router.get("", response_model=list[RuleOut])
async def list_rules(
    site_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[RuleOut]:
    require_permission(context, "rule.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="r.site_id")
    clauses = [scope_clause]
    params: dict = {"limit": limit, **scope_params}
    if site_id:
        clauses.append("r.site_id = :site_id")
        params["site_id"] = site_id
    if camera_id:
        # Site-wide rules apply to this camera too, so they belong in the answer.
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
    rule_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> RuleOut:
    require_permission(context, "rule.read")
    return await _load(db, rule_id)


@router.post("", response_model=RuleOut, status_code=201)
async def create_rule(
    body: RuleIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> RuleOut:
    require_permission(context, "rule.manage")
    await _check_references(
        db, site_id=body.site_id, camera_id=body.camera_id, zone_id=body.zone_id
    )

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
                "tenant_id": context.tenant_id,
                "site_id": body.site_id,
                "camera_id": body.camera_id,
                "zone_id": body.zone_id,
                "name": body.name,
                "type_code": body.type_code,
                "classes": json.dumps(body.alertable_classes),
                "min_confidence": body.min_confidence,
                "severity": body.severity,
                "min_roi_overlap": body.min_roi_overlap,
                "min_consecutive_frames": body.min_consecutive_frames,
                "cooldown_seconds": body.cooldown_seconds,
                "active_from_hour": body.active_from_hour,
                "active_to_hour": body.active_to_hour,
            },
        )
    ).scalar_one()

    logger.info(
        "detection_rule_created",
        extra={
            "rule_id": str(rule_id),
            "type_code": body.type_code,
            "min_confidence": body.min_confidence,
            "scope": "camera" if body.camera_id else "site",
        },
    )
    return await _load(db, rule_id)


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    body: RulePatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> RuleOut:
    require_permission(context, "rule.manage")
    existing = await _load(db, rule_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return existing

    await _check_references(
        db,
        site_id=existing.site_id,
        camera_id=changes.get("camera_id", existing.camera_id),
        zone_id=changes.get("zone_id", existing.zone_id),
    )

    assignments = []
    params: dict = {"id": rule_id}
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
            "version = version + 1 WHERE id = :id"
        ),
        params,
    )

    if "status" in changes:
        # Its own line. Disabling a rule stops alerts arriving, and "why did we stop
        # getting told" needs to be answerable without reading a diff of this table.
        logger.info(
            "detection_rule_status_changed",
            extra={"rule_id": str(rule_id), "status": changes["status"]},
        )
    logger.info(
        "detection_rule_updated",
        extra={"rule_id": str(rule_id), "fields": sorted(changes)},
    )
    return await _load(db, rule_id)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Deletes a rule outright.

    No soft delete here, unlike cameras and sites. Nothing references a rule after the
    fact - incidents record what fired at the time, not a live pointer - so keeping a
    disabled row would only make the list harder to read. Disabling is the reversible
    option and it is one field away.
    """
    require_permission(context, "rule.manage")
    rule = await _load(db, rule_id)

    await db.execute(text("DELETE FROM detection_rules WHERE id = :id"), {"id": rule_id})
    # "name" is reserved on LogRecord (it is the logger's own name) - passing it in
    # `extra` raises inside the logging module itself, turning a routine delete into a
    # 500. Every field here is namespaced with rule_ instead, which is also just clearer.
    logger.info(
        "detection_rule_deleted",
        extra={"rule_id": str(rule_id), "rule_name": rule.name},
    )
