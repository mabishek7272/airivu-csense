"""Tenant-facing incident endpoints (TRD §10.2).

Every query here runs inside `tenant_session()`, so row-level security scopes results to
the caller's tenant at the database level. The handlers never filter by tenant_id
themselves and never accept one from the client - a bug in this file cannot leak another
tenant's incidents, because the policy is enforced below the application.

Cursor pagination rather than offset (TRD §10.1): an incident inbox receives new rows
constantly, and offset paging silently skips or repeats items when the underlying set
shifts between pages.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.pipeline.incidents import InvalidTransitionError, transition_incident
from csense_shared.security.permissions import require_permission
from csense_shared.security.site_scope import site_scope_sql_filter
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/incidents", tags=["incidents"])

MAX_PAGE_SIZE = 100

# Statuses that still represent work in front of a human. An acknowledged or escalated
# incident is not finished - filtering the inbox to `open` alone makes an incident vanish
# the moment someone acknowledges it, which is precisely when it becomes their job.
ACTIVE_STATUSES = ("open", "acknowledged", "investigating", "escalated")
VALID_STATUSES = frozenset(ACTIVE_STATUSES) | {"resolved", "dismissed"}


class IncidentSummary(BaseModel):
    id: str
    incident_number: int
    type_code: str
    severity: str
    status: str
    title: str
    summary: str | None
    camera_id: str
    site_id: str
    detection_count: int
    first_detected_at: dt.datetime
    last_detected_at: dt.datetime
    acknowledged_at: dt.datetime | None


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    next_cursor: str | None = None


class IncidentEventOut(BaseModel):
    event_type: str
    actor_type: str
    actor_id: str | None
    previous_status: str | None
    new_status: str | None
    occurred_at: dt.datetime
    payload: dict


class IncidentDetail(IncidentSummary):
    resolution_code: str | None
    resolution_summary: str | None
    metadata: dict
    events: list[IncidentEventOut]
    detection_ids: list[str]


class TransitionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ResolveRequest(BaseModel):
    resolution_code: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


def _encode_cursor(last_detected_at: dt.datetime, incident_id: uuid.UUID) -> str:
    payload = json.dumps({"t": last_detected_at.isoformat(), "id": str(incident_id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return dt.datetime.fromisoformat(payload["t"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ApiError(
            status_code=400,
            code="invalid_cursor",
            message="Pagination cursor is malformed.",
        ) from exc


@router.get("", response_model=IncidentPage)
async def list_incidents(
    status: str | None = Query(
        default=None,
        description=(
            "Comma-separated statuses, or 'active' for everything not yet closed. "
            "Omit for all."
        ),
    ),
    severity: str | None = Query(default=None),
    camera_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> IncidentPage:
    require_permission(context, "incident.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="site_id")
    filters = [scope_clause]
    params: dict = {"limit": limit + 1, **scope_params}  # one extra row tells us whether more exist

    if status:
        wanted = ACTIVE_STATUSES if status == "active" else tuple(
            part.strip() for part in status.split(",") if part.strip()
        )
        invalid = [s for s in wanted if s not in VALID_STATUSES]
        if invalid:
            raise ApiError(
                status_code=400,
                code="invalid_status",
                message=f"Unknown status: {', '.join(invalid)}.",
                details={"valid": sorted(VALID_STATUSES)},
            )
        filters.append("status = ANY(CAST(:statuses AS incident_status[]))")
        params["statuses"] = list(wanted)
    if severity:
        filters.append("severity = CAST(:severity AS incident_severity)")
        params["severity"] = severity
    if camera_id:
        filters.append("camera_id = :camera_id")
        params["camera_id"] = camera_id
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        # Keyset pagination on the same (time, id) tuple the ordering uses, so a row
        # arriving mid-scroll cannot cause a skip or a repeat.
        filters.append("(last_detected_at, id) < (:cursor_time, :cursor_id)")
        params["cursor_time"] = cursor_time
        params["cursor_id"] = cursor_id

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    result = await db.execute(
        text(
            f"""
            SELECT id, incident_number, type_code, severity::text, status::text, title, summary,
                   camera_id, site_id, detection_count, first_detected_at, last_detected_at,
                   acknowledged_at
            FROM incidents
            {where}
            ORDER BY last_detected_at DESC, id DESC
            LIMIT :limit
            """
        ),
        params,
    )
    rows = result.all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        IncidentSummary(
            id=str(r[0]), incident_number=r[1], type_code=r[2], severity=r[3], status=r[4],
            title=r[5], summary=r[6], camera_id=str(r[7]), site_id=str(r[8]),
            detection_count=r[9], first_detected_at=r[10], last_detected_at=r[11],
            acknowledged_at=r[12],
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][11], rows[-1][0]) if has_more and rows else None
    return IncidentPage(items=items, next_cursor=next_cursor)


@router.get("/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> IncidentDetail:
    require_permission(context, "incident.read")

    row = (
        await db.execute(
            text(
                """
                SELECT id, incident_number, type_code, severity::text, status::text, title, summary,
                       camera_id, site_id, detection_count, first_detected_at, last_detected_at,
                       acknowledged_at, resolution_code, resolution_summary, metadata
                FROM incidents WHERE id = :id
                """
            ),
            {"id": incident_id},
        )
    ).first()
    # RLS already scoped this query, so "not found" covers both a missing incident and one
    # belonging to another tenant - deliberately indistinguishable, so this endpoint cannot
    # be used to probe for the existence of other tenants' records.
    if row is None:
        raise NotFoundError("Incident not found.")
    # Same "not found covers both" shape, now also covering a real incident outside a
    # selected-scoped member's assigned sites - see sites.py/cameras.py/zones.py/
    # rules.py's own identical checks.
    if not context.can_access_site(row[8]):
        raise NotFoundError("Incident not found.")

    events = (
        await db.execute(
            text(
                """
                SELECT event_type, actor_type, actor_id, previous_status, new_status,
                       occurred_at, payload
                FROM incident_events WHERE incident_id = :id ORDER BY occurred_at, id
                """
            ),
            {"id": incident_id},
        )
    ).all()

    detections = (
        await db.execute(
            text(
                "SELECT detection_id FROM incident_detection_links "
                "WHERE incident_id = :id ORDER BY capture_time LIMIT 500"
            ),
            {"id": incident_id},
        )
    ).scalars().all()

    return IncidentDetail(
        id=str(row[0]), incident_number=row[1], type_code=row[2], severity=row[3], status=row[4],
        title=row[5], summary=row[6], camera_id=str(row[7]), site_id=str(row[8]),
        detection_count=row[9], first_detected_at=row[10], last_detected_at=row[11],
        acknowledged_at=row[12], resolution_code=row[13], resolution_summary=row[14],
        metadata=row[15] or {},
        events=[
            IncidentEventOut(
                event_type=e[0], actor_type=e[1], actor_id=e[2], previous_status=e[3],
                new_status=e[4], occurred_at=e[5], payload=e[6] or {},
            )
            for e in events
        ],
        detection_ids=[str(d) for d in detections],
    )


async def _transition(
    request: Request,
    db: AsyncSession,
    context: TenantContext,
    incident_id: uuid.UUID,
    new_status: str,
    reason: str | None,
    resolution_code: str | None = None,
) -> dict:
    # Same "not found covers both" shape get_incident already applies - a
    # selected-scoped member acknowledging/investigating/resolving/dismissing an
    # incident outside their assigned sites sees the same response as a genuinely
    # missing one, checked before any state actually changes.
    site_row = (
        await db.execute(text("SELECT site_id FROM incidents WHERE id = :id"), {"id": incident_id})
    ).first()
    if site_row is not None and not context.can_access_site(site_row[0]):
        raise NotFoundError("Incident not found.")

    correlation_id = getattr(request.state, "correlation_id", None)
    try:
        previous = await transition_incident(
            db,
            tenant_id=context.tenant_id,
            incident_id=incident_id,
            new_status=new_status,
            actor_type="user",
            actor_id=str(context.user_id),
            reason=reason,
            resolution_code=resolution_code,
            correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
        )
    except InvalidTransitionError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise NotFoundError("Incident not found.") from exc
        raise ConflictError(message) from exc

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action=f"incident.{new_status}",
        outcome="success",
        target_type="incident",
        target_id=str(incident_id),
        reason=reason,
        before_patch={"status": previous},
        after_patch={"status": new_status},
        correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
        event_type=f"incident.{new_status}.v1",
        event_payload={"incident_id": str(incident_id), "previous_status": previous},
        aggregate_type="incident",
        aggregate_id=str(incident_id),
    )
    return {"id": str(incident_id), "previous_status": previous, "status": new_status}


@router.post("/{incident_id}/acknowledge")
async def acknowledge_incident(
    incident_id: uuid.UUID,
    body: TransitionRequest,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> dict:
    require_permission(context, "incident.acknowledge")
    return await _transition(request, db, context, incident_id, "acknowledged", body.reason)


@router.post("/{incident_id}/investigate")
async def investigate_incident(
    incident_id: uuid.UUID,
    body: TransitionRequest,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> dict:
    require_permission(context, "incident.acknowledge")
    return await _transition(request, db, context, incident_id, "investigating", body.reason)


@router.post("/{incident_id}/resolve")
async def resolve_incident(
    incident_id: uuid.UUID,
    body: ResolveRequest,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> dict:
    require_permission(context, "incident.close")
    return await _transition(
        request, db, context, incident_id, "resolved", body.reason, body.resolution_code
    )


@router.post("/{incident_id}/dismiss")
async def dismiss_incident(
    incident_id: uuid.UUID,
    body: ResolveRequest,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> dict:
    require_permission(context, "incident.close")
    return await _transition(
        request, db, context, incident_id, "dismissed", body.reason, body.resolution_code
    )
