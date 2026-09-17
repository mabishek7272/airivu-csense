"""Async report/export jobs (CHECKLIST: "Async reports/exports with time-limited
download"). Three export types: incidents, detections, and audit-events, all CSV. `POST`
returns a `queued` job immediately; the actual query + CSV build + MinIO upload runs in a
FastAPI `BackgroundTasks` callback, off the request path, using its own DB session -
FastAPI runs background tasks *before* a dependency's post-`yield` cleanup (confirmed
directly against the installed FastAPI's own `routing.py`, not assumed), so the request's
own transaction is still open when the task starts; each `request_*_export` commits it
explicitly before scheduling its task so the freshly-inserted row is actually visible to
the task's own, separate session (see each one's own comment).

**Detections, then audit-events, were the CHECKLIST-named "easy next slice"** after
incidents - same `export_jobs`/background-task/presigned-download mechanism each time,
just a new `export_type` and its own query-building function, mirroring the equivalent
list endpoint's own filter vocabulary exactly so an operator who already knows that
endpoint's filters doesn't have to learn a second set for the export
(`detections.list_detections`'s `camera_id`/`site_id`/`event_type`/`since`/`until`/
`min_confidence`; `audit.list_audit_events`'s `action`/`target_type`/`outcome`/`since`/
`until`).

**A first draft assumed `audit.read` was `tenant_owner`-only** (misreading
`app.api.audit`'s own docstring, which contrasts the *customer* audience's `tenant_owner`
against the *platform* audience's `platform_admin` - not `tenant_owner` against
`tenant_member` within the customer audience) and built a per-export-type permission gate
into `list_exports`/`get_export` on that basis. **A real e2e run against the live
`role_permissions` table caught the mistake**: `tenant_member` currently holds both
`incident.read` and `audit.read` (confirmed by direct query, not re-assumed from prose),
so there is no real permission gap between incidents/detections and audit-events today.
The gate (`_EXPORT_TYPE_PERMISSION`, see its own comment) is kept anyway, not removed -
it costs nothing while the two permissions happen to coincide, and stops `list_exports`/
`get_export` from silently over-sharing if a future change ever narrows `audit.read`
without anyone thinking to update this file too. It is correctness insurance for a gap
that does not exist *yet*, not a fix for one that does.

No new permission for incidents/detections - see migration 0050's own docstring for why
exporting what you can already read doesn't need one. audit-events reuses `audit.read`
for the same reason.

Download is a presigned URL minted fresh on every `GET .../{id}` (`EXPORT_DOWNLOAD_TTL`,
short-lived, same reasoning as evidence images), gated by the job's own longer-lived
`expires_at` (`EXPORT_FILE_RETENTION` after completion) - lazily flipped to `expired` on
read, the same precedent `sync_license_status` and support-grant reads already
established rather than a scheduled cleanup job this deployment has nowhere to run. The
object itself is left in MinIO past that point (cheap, and bucket lifecycle policy is an
infra-level concern - SCH §14) - only the API stops handing out fresh links to it.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from minio import Minio
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.incidents import ACTIVE_STATUSES, VALID_STATUSES
from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from csense_shared.config import Settings
from csense_shared.db.postgres import tenant_session
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.exports import (
    build_audit_events_csv,
    build_detections_csv,
    build_incidents_csv,
    summarize_objects,
)
from csense_shared.logging import get_logger
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext
from csense_shared.storage.objects import BUCKET_EXPORTS, create_presign_client, tenant_export_key

router = APIRouter(prefix="/api/v1/tenant/exports", tags=["exports"])
logger = get_logger(__name__)

EXPORT_FILE_RETENTION = dt.timedelta(hours=24)
EXPORT_DOWNLOAD_TTL = dt.timedelta(minutes=15)
MAX_EXPORT_ROWS = 50_000

_SELECT_FIELDS = """
    id, incident_number, type_code, severity::text, status::text, title, summary,
    camera_id, site_id, detection_count, first_detected_at, last_detected_at,
    acknowledged_at, resolution_code, resolution_summary
"""

# Same join shape as `detections.py`'s own `_BASE_SELECT` (deliberately re-derived here,
# not imported - that module's SELECT also carries columns an export row has no use for,
# like `d.objects` needing its own JSONB->CSV flattening rather than a `DetectionOut`
# pydantic shape) but the same tables/joins, so an export and the live detections list
# agree on what "this detection's camera/site/zone/incident" means.
_DETECTION_SELECT_FIELDS = """
    d.id, d.event_type, d.source_event_id, d.confidence, d.capture_time, d.cloud_receive_time,
    d.objects, d.camera_id, c.name, d.site_id, s.name, z.name,
    i.id, i.incident_number
"""
_DETECTION_JOINS = """
    FROM detections d
    JOIN cameras c ON c.id = d.camera_id
    JOIN sites s ON s.id = d.site_id
    LEFT JOIN zones z ON z.id = c.zone_id
    LEFT JOIN incident_detection_links l ON l.detection_id = d.id
    LEFT JOIN incidents i ON i.id = l.incident_id
"""

# Same friendly-actor-name join as `app.api.audit.list_audit_events` (see that module's
# own docstring for the full reasoning - a single `users` join covers both
# actor_type="user" and "platform_developer", and why). Re-derived here rather than
# imported: that endpoint's own SELECT also carries pagination/filter concerns this
# export has no use for.
_AUDIT_EVENT_SELECT_FIELDS = """
    ae.id, ae.occurred_at, ae.actor_type, ae.actor_id, actor_user.display_name,
    ae.action, ae.target_type, ae.target_id, ae.outcome::text, ae.reason
"""
_AUDIT_EVENT_JOINS = """
    FROM audit_events ae
    LEFT JOIN users actor_user
        ON ae.actor_type IN ('user', 'platform_developer') AND ae.actor_id = actor_user.id::text
"""


class RequestIncidentsExportIn(BaseModel):
    status: str | None = Field(default=None, description="Comma-separated statuses, or 'active'. Omit for all.")
    severity: str | None = None
    camera_id: uuid.UUID | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None


class RequestDetectionsExportIn(BaseModel):
    """Mirrors `detections.list_detections`'s own filter vocabulary exactly (see that
    endpoint's own Query params) - `min_confidence`, not the incident export's
    `severity`, since detections and incidents don't share a severity concept."""

    camera_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    event_type: str | None = None
    since: dt.datetime | None = Field(default=None, description="Filter on source capture time")
    until: dt.datetime | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)


class RequestAuditEventsExportIn(BaseModel):
    """Mirrors `audit.list_audit_events`'s own filter vocabulary exactly (see that
    endpoint's own Query params)."""

    action: str | None = Field(default=None, description="Exact action, e.g. camera.manage")
    target_type: str | None = None
    outcome: str | None = Field(default=None, pattern="^(success|failure)$")
    since: dt.datetime | None = None
    until: dt.datetime | None = None


class ExportJobOut(BaseModel):
    id: str
    export_type: str
    status: str
    row_count: int | None
    error_message: str | None
    requested_at: str
    completed_at: str | None
    expires_at: str | None
    download_url: str | None = None


def _to_out(row, download_url: str | None = None) -> ExportJobOut:
    return ExportJobOut(
        id=str(row[0]), export_type=row[1], status=row[2], row_count=row[3],
        error_message=row[4], requested_at=row[5].isoformat(),
        completed_at=row[6].isoformat() if row[6] else None,
        expires_at=row[7].isoformat() if row[7] else None,
        download_url=download_url,
    )


_JOB_SELECT = """
    SELECT id, export_type, status::text, row_count, error_message, requested_at,
           completed_at, expires_at, object_key
    FROM export_jobs
"""


def _build_incident_filters(body: RequestIncidentsExportIn) -> tuple[list[str], dict]:
    """Mirrors `incidents.list_incidents`'s own filter-building (same statuses/severity/
    camera_id semantics an operator already relies on there), plus a `first_detected_at`
    date range that endpoint has no reason to offer but an export naturally does."""
    filters: list[str] = []
    params: dict = {}

    if body.status:
        wanted = ACTIVE_STATUSES if body.status == "active" else tuple(
            part.strip() for part in body.status.split(",") if part.strip()
        )
        invalid = [s for s in wanted if s not in VALID_STATUSES]
        if invalid:
            raise ApiError(
                status_code=400, code="invalid_status",
                message=f"Unknown status: {', '.join(invalid)}.",
                details={"valid": sorted(VALID_STATUSES)},
            )
        filters.append("status = ANY(CAST(:statuses AS incident_status[]))")
        params["statuses"] = list(wanted)
    if body.severity:
        filters.append("severity = CAST(:severity AS incident_severity)")
        params["severity"] = body.severity
    if body.camera_id:
        filters.append("camera_id = :camera_id")
        params["camera_id"] = body.camera_id
    if body.since:
        filters.append("first_detected_at >= :since")
        params["since"] = body.since
    if body.until:
        filters.append("first_detected_at <= :until")
        params["until"] = body.until

    return filters, params


def _build_detection_filters(body: RequestDetectionsExportIn) -> tuple[list[str], dict]:
    """Mirrors `detections.list_detections`'s own filter-building exactly - `d.`-qualified
    the same way that endpoint's own query is, since `_DETECTION_JOINS` brings in
    `cameras`/`sites`/`zones`/`incidents` too."""
    filters: list[str] = []
    params: dict = {}

    if body.camera_id:
        filters.append("d.camera_id = :camera_id")
        params["camera_id"] = body.camera_id
    if body.site_id:
        filters.append("d.site_id = :site_id")
        params["site_id"] = body.site_id
    if body.event_type:
        filters.append("d.event_type = :event_type")
        params["event_type"] = body.event_type
    if body.since:
        filters.append("d.capture_time >= :since")
        params["since"] = body.since
    if body.until:
        filters.append("d.capture_time <= :until")
        params["until"] = body.until
    if body.min_confidence is not None:
        filters.append("d.confidence >= :min_confidence")
        params["min_confidence"] = body.min_confidence

    return filters, params


def _build_audit_event_filters(body: RequestAuditEventsExportIn) -> tuple[list[str], dict]:
    """Mirrors `audit.list_audit_events`'s own filter-building exactly - `ae.`-qualified
    the same way that endpoint's own query is (and for the same reason: `_AUDIT_EVENT_
    JOINS` adds `users`, which also has its own `id` column)."""
    filters: list[str] = []
    params: dict = {}

    if body.action:
        filters.append("ae.action = :action")
        params["action"] = body.action
    if body.target_type:
        filters.append("ae.target_type = :target_type")
        params["target_type"] = body.target_type
    if body.outcome:
        filters.append("ae.outcome = CAST(:outcome AS audit_outcome)")
        params["outcome"] = body.outcome
    if body.since:
        filters.append("ae.occurred_at >= :since")
        params["since"] = body.since
    if body.until:
        filters.append("ae.occurred_at <= :until")
        params["until"] = body.until

    return filters, params


async def _run_incidents_export(
    *,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    filters: list[str],
    params: dict,
    session_factory: async_sessionmaker[AsyncSession],
    object_store,
) -> None:
    """Runs after the response is sent, but (per this module's own docstring) before the
    request's own DB session is torn down - it never touches that session, only
    `tenant_session`'s own fresh one, the same RLS-scoping helper `db_session_for_tenant`
    uses on the request path, just called directly instead of as a FastAPI dependency.
    `object_store` is the one long-lived MinIO client the app already keeps on
    `app.state` (main.py's own lifespan) - safe to reuse here, it holds no per-request
    state."""
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    try:
        async with tenant_session(session_factory, tenant_id) as db:
            await db.execute(
                text("UPDATE export_jobs SET status = 'processing' WHERE id = :id"), {"id": job_id}
            )

            count_row = await db.execute(text(f"SELECT count(*) FROM incidents {where}"), params)
            total = count_row.scalar_one()

            result = await db.execute(
                text(
                    f"""
                    SELECT {_SELECT_FIELDS}
                    FROM incidents
                    {where}
                    ORDER BY last_detected_at DESC, id DESC
                    LIMIT :cap
                    """
                ),
                {**params, "cap": MAX_EXPORT_ROWS},
            )
            rows = result.all()

            csv_rows = [
                {
                    "id": str(r[0]), "incident_number": r[1], "type_code": r[2], "severity": r[3],
                    "status": r[4], "title": r[5], "summary": r[6], "camera_id": str(r[7]),
                    "site_id": str(r[8]), "detection_count": r[9], "first_detected_at": r[10],
                    "last_detected_at": r[11], "acknowledged_at": r[12], "resolution_code": r[13],
                    "resolution_summary": r[14],
                }
                for r in rows
            ]
            csv_bytes = build_incidents_csv(csv_rows)

            object_key = tenant_export_key(tenant_id, "incidents", job_id)
            if object_store is not None:
                object_store.put_object(
                    BUCKET_EXPORTS, object_key, io.BytesIO(csv_bytes), length=len(csv_bytes),
                    content_type="text/csv",
                )
            else:
                raise RuntimeError("Object storage is unavailable.")

            error_message = (
                f"Truncated to the most recent {MAX_EXPORT_ROWS} of {total} matching incidents."
                if total > MAX_EXPORT_ROWS else None
            )
            now = dt.datetime.now(dt.UTC)
            await db.execute(
                text(
                    "UPDATE export_jobs SET status = 'completed', row_count = :row_count, "
                    "object_key = :object_key, error_message = :error_message, "
                    "completed_at = :now, expires_at = :expires_at WHERE id = :id"
                ),
                {
                    "row_count": len(csv_rows), "object_key": object_key, "error_message": error_message,
                    "now": now, "expires_at": now + EXPORT_FILE_RETENTION, "id": job_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("export_job_failed", extra={"job_id": str(job_id)})
        try:
            async with tenant_session(session_factory, tenant_id) as db:
                await db.execute(
                    text(
                        "UPDATE export_jobs SET status = 'failed', "
                        "error_message = 'Export generation failed.' WHERE id = :id"
                    ),
                    {"id": job_id},
                )
        except Exception:  # noqa: BLE001
            logger.exception("export_job_failure_status_update_also_failed", extra={"job_id": str(job_id)})


async def _run_detections_export(
    *,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    filters: list[str],
    params: dict,
    session_factory: async_sessionmaker[AsyncSession],
    object_store,
) -> None:
    """Same shape and same real ordering guarantee as `_run_incidents_export` (see that
    function's own docstring) - not merged into one generic function, so each export
    type's own SELECT/CSV-build stays simple to read top-to-bottom rather than threaded
    through a shared abstraction for two call sites."""
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    try:
        async with tenant_session(session_factory, tenant_id) as db:
            await db.execute(
                text("UPDATE export_jobs SET status = 'processing' WHERE id = :id"), {"id": job_id}
            )

            count_row = await db.execute(
                text(f"SELECT count(*) {_DETECTION_JOINS} {where}"), params
            )
            total = count_row.scalar_one()

            result = await db.execute(
                text(
                    f"""
                    SELECT {_DETECTION_SELECT_FIELDS}
                    {_DETECTION_JOINS}
                    {where}
                    ORDER BY d.capture_time DESC, d.id DESC
                    LIMIT :cap
                    """
                ),
                {**params, "cap": MAX_EXPORT_ROWS},
            )
            rows = result.all()

            csv_rows = [
                {
                    "id": str(r[0]), "event_type": r[1], "source_event_id": r[2], "confidence": r[3],
                    "captured_at": r[4], "cloud_received_at": r[5],
                    "camera_id": str(r[7]), "camera_name": r[8],
                    "site_id": str(r[9]), "site_name": r[10], "zone_name": r[11],
                    "object_count": len(r[6] or []), "objects_summary": summarize_objects(r[6]),
                    "incident_id": str(r[12]) if r[12] else None, "incident_number": r[13],
                }
                for r in rows
            ]
            csv_bytes = build_detections_csv(csv_rows)

            object_key = tenant_export_key(tenant_id, "detections", job_id)
            if object_store is not None:
                object_store.put_object(
                    BUCKET_EXPORTS, object_key, io.BytesIO(csv_bytes), length=len(csv_bytes),
                    content_type="text/csv",
                )
            else:
                raise RuntimeError("Object storage is unavailable.")

            error_message = (
                f"Truncated to the most recent {MAX_EXPORT_ROWS} of {total} matching detections."
                if total > MAX_EXPORT_ROWS else None
            )
            now = dt.datetime.now(dt.UTC)
            await db.execute(
                text(
                    "UPDATE export_jobs SET status = 'completed', row_count = :row_count, "
                    "object_key = :object_key, error_message = :error_message, "
                    "completed_at = :now, expires_at = :expires_at WHERE id = :id"
                ),
                {
                    "row_count": len(csv_rows), "object_key": object_key, "error_message": error_message,
                    "now": now, "expires_at": now + EXPORT_FILE_RETENTION, "id": job_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("export_job_failed", extra={"job_id": str(job_id)})
        try:
            async with tenant_session(session_factory, tenant_id) as db:
                await db.execute(
                    text(
                        "UPDATE export_jobs SET status = 'failed', "
                        "error_message = 'Export generation failed.' WHERE id = :id"
                    ),
                    {"id": job_id},
                )
        except Exception:  # noqa: BLE001
            logger.exception("export_job_failure_status_update_also_failed", extra={"job_id": str(job_id)})


async def _run_audit_events_export(
    *,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    filters: list[str],
    params: dict,
    session_factory: async_sessionmaker[AsyncSession],
    object_store,
) -> None:
    """Same shape and same real ordering guarantee as `_run_incidents_export` (see that
    function's own docstring)."""
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    try:
        async with tenant_session(session_factory, tenant_id) as db:
            await db.execute(
                text("UPDATE export_jobs SET status = 'processing' WHERE id = :id"), {"id": job_id}
            )

            count_row = await db.execute(
                text(f"SELECT count(*) {_AUDIT_EVENT_JOINS} {where}"), params
            )
            total = count_row.scalar_one()

            result = await db.execute(
                text(
                    f"""
                    SELECT {_AUDIT_EVENT_SELECT_FIELDS}
                    {_AUDIT_EVENT_JOINS}
                    {where}
                    ORDER BY ae.occurred_at DESC, ae.id DESC
                    LIMIT :cap
                    """
                ),
                {**params, "cap": MAX_EXPORT_ROWS},
            )
            rows = result.all()

            csv_rows = [
                {
                    "id": str(r[0]), "occurred_at": r[1], "actor_type": r[2], "actor_id": r[3],
                    "actor_display_name": r[4], "action": r[5], "target_type": r[6],
                    "target_id": r[7], "outcome": r[8], "reason": r[9],
                }
                for r in rows
            ]
            csv_bytes = build_audit_events_csv(csv_rows)

            object_key = tenant_export_key(tenant_id, "audit-events", job_id)
            if object_store is not None:
                object_store.put_object(
                    BUCKET_EXPORTS, object_key, io.BytesIO(csv_bytes), length=len(csv_bytes),
                    content_type="text/csv",
                )
            else:
                raise RuntimeError("Object storage is unavailable.")

            error_message = (
                f"Truncated to the most recent {MAX_EXPORT_ROWS} of {total} matching audit events."
                if total > MAX_EXPORT_ROWS else None
            )
            now = dt.datetime.now(dt.UTC)
            await db.execute(
                text(
                    "UPDATE export_jobs SET status = 'completed', row_count = :row_count, "
                    "object_key = :object_key, error_message = :error_message, "
                    "completed_at = :now, expires_at = :expires_at WHERE id = :id"
                ),
                {
                    "row_count": len(csv_rows), "object_key": object_key, "error_message": error_message,
                    "now": now, "expires_at": now + EXPORT_FILE_RETENTION, "id": job_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("export_job_failed", extra={"job_id": str(job_id)})
        try:
            async with tenant_session(session_factory, tenant_id) as db:
                await db.execute(
                    text(
                        "UPDATE export_jobs SET status = 'failed', "
                        "error_message = 'Export generation failed.' WHERE id = :id"
                    ),
                    {"id": job_id},
                )
        except Exception:  # noqa: BLE001
            logger.exception("export_job_failure_status_update_also_failed", extra={"job_id": str(job_id)})


@router.post("/incidents", response_model=ExportJobOut, status_code=202)
async def request_incidents_export(
    body: RequestIncidentsExportIn,
    request: Request,
    background_tasks: BackgroundTasks,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ExportJobOut:
    require_permission(context, "incident.read")

    filters, params = _build_incident_filters(body)
    filters_json = body.model_dump(mode="json", exclude_none=True)

    new_id = (
        await db.execute(
            text(
                "INSERT INTO export_jobs (tenant_id, export_type, filters, requested_by) "
                "VALUES (:tenant_id, 'incidents', CAST(:filters AS jsonb), :requested_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": context.tenant_id, "requested_by": context.user_id,
                "filters": json.dumps(filters_json),
            },
        )
    ).scalar_one()
    row = (await db.execute(text(f"{_JOB_SELECT} WHERE id = :id"), {"id": new_id})).first()

    # Committing before scheduling the background task matters: the task opens its own
    # session and would not see an uncommitted INSERT from this one.
    await db.commit()

    background_tasks.add_task(
        _run_incidents_export,
        job_id=new_id,
        tenant_id=context.tenant_id,
        filters=filters,
        params=params,
        session_factory=request.app.state.session_factory,
        object_store=request.app.state.object_store,
    )

    return _to_out(row[:8])


@router.post("/detections", response_model=ExportJobOut, status_code=202)
async def request_detections_export(
    body: RequestDetectionsExportIn,
    request: Request,
    background_tasks: BackgroundTasks,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ExportJobOut:
    require_permission(context, "incident.read")

    filters, params = _build_detection_filters(body)
    filters_json = body.model_dump(mode="json", exclude_none=True)

    new_id = (
        await db.execute(
            text(
                "INSERT INTO export_jobs (tenant_id, export_type, filters, requested_by) "
                "VALUES (:tenant_id, 'detections', CAST(:filters AS jsonb), :requested_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": context.tenant_id, "requested_by": context.user_id,
                "filters": json.dumps(filters_json),
            },
        )
    ).scalar_one()
    row = (await db.execute(text(f"{_JOB_SELECT} WHERE id = :id"), {"id": new_id})).first()

    # Same reason as request_incidents_export: the background task opens its own session
    # and would not see an uncommitted INSERT from this one.
    await db.commit()

    background_tasks.add_task(
        _run_detections_export,
        job_id=new_id,
        tenant_id=context.tenant_id,
        filters=filters,
        params=params,
        session_factory=request.app.state.session_factory,
        object_store=request.app.state.object_store,
    )

    return _to_out(row[:8])


@router.post("/audit-events", response_model=ExportJobOut, status_code=202)
async def request_audit_events_export(
    body: RequestAuditEventsExportIn,
    request: Request,
    background_tasks: BackgroundTasks,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ExportJobOut:
    # audit.read, not incident.read: matches app.api.audit's own gate exactly - see this
    # module's own docstring for a real, corrected note on what that permission's actual
    # grant looks like (tenant_member holds it too, same as incident.read).
    require_permission(context, "audit.read")

    filters, params = _build_audit_event_filters(body)
    filters_json = body.model_dump(mode="json", exclude_none=True)

    new_id = (
        await db.execute(
            text(
                "INSERT INTO export_jobs (tenant_id, export_type, filters, requested_by) "
                "VALUES (:tenant_id, 'audit-events', CAST(:filters AS jsonb), :requested_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": context.tenant_id, "requested_by": context.user_id,
                "filters": json.dumps(filters_json),
            },
        )
    ).scalar_one()
    row = (await db.execute(text(f"{_JOB_SELECT} WHERE id = :id"), {"id": new_id})).first()

    # Same reason as request_incidents_export: the background task opens its own session
    # and would not see an uncommitted INSERT from this one.
    await db.commit()

    background_tasks.add_task(
        _run_audit_events_export,
        job_id=new_id,
        tenant_id=context.tenant_id,
        filters=filters,
        params=params,
        session_factory=request.app.state.session_factory,
        object_store=request.app.state.object_store,
    )

    return _to_out(row[:8])


# `list_exports`/`get_export` read across every export type in one place. Each export
# type is gated by its own real permission (see this module's own docstring for the real,
# corrected note on what those permissions actually grant today - `tenant_member` holds
# both `incident.read` and `audit.read` right now, so this is not currently closing a live
# gap) - kept as real correctness insurance rather than removed, so a future change to
# either permission's grant can't silently over-share through this shared read path
# without anyone having to remember these two endpoints exist.
_EXPORT_TYPE_PERMISSION = {
    "incidents": "incident.read",
    "detections": "incident.read",
    "audit-events": "audit.read",
}


@router.get("", response_model=list[ExportJobOut])
async def list_exports(
    limit: int = Query(default=25, ge=1, le=100),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ExportJobOut]:
    require_permission(context, "incident.read")
    await db.execute(
        text(
            "UPDATE export_jobs SET status = 'expired' "
            "WHERE status = 'completed' AND expires_at < now()"
        )
    )
    rows = (
        await db.execute(text(f"{_JOB_SELECT} ORDER BY requested_at DESC LIMIT :limit"), {"limit": limit})
    ).all()
    # Drop any row this caller lacks the per-type permission for (see
    # `_EXPORT_TYPE_PERMISSION`'s own comment) - filtered out of the list entirely, not
    # merely hidden from the download link.
    visible = [r for r in rows if context.has_permission(_EXPORT_TYPE_PERMISSION.get(r[1], "incident.read"))]
    return [_to_out(r[:8]) for r in visible]


def _presign_download(settings: Settings, object_key: str) -> str:
    minio: Minio = create_presign_client(settings)
    return minio.presigned_get_object(BUCKET_EXPORTS, object_key, expires=EXPORT_DOWNLOAD_TTL)


@router.get("/{job_id}", response_model=ExportJobOut)
async def get_export(
    job_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> ExportJobOut:
    require_permission(context, "incident.read")

    # Lazy expiry, run before the read that answers this request - see this module's own
    # docstring.
    await db.execute(
        text(
            "UPDATE export_jobs SET status = 'expired' "
            "WHERE id = :id AND status = 'completed' AND expires_at < now()"
        ),
        {"id": job_id},
    )
    row = (await db.execute(text(f"{_JOB_SELECT} WHERE id = :id"), {"id": job_id})).first()
    # Same "not found covers both missing and unauthorized" convention `detections.py`'s
    # own `get_detection` already uses for RLS - here it's a permission check instead of
    # RLS, but the same non-probeable-existence reasoning applies: whoever lacks the
    # permission this job's export_type requires should not learn the job exists either.
    if row is None or not context.has_permission(_EXPORT_TYPE_PERMISSION.get(row[1], "incident.read")):
        raise NotFoundError("No such export job.")

    download_url = None
    if row[2] == "completed" and row[8]:
        download_url = _presign_download(settings, row[8])

    return _to_out(row[:8], download_url=download_url)
