"""Cross-tenant audit trail for platform operators - `audit.read`, `platform_admin`.

Same shared permission code as the tenant-facing `GET /api/v1/tenant/audit-events`
(migration 0036) - `TenantContext`/`PlatformContext` are separate types resolved from
separate audience-scoped tokens, so a platform token holding `audit.read` carries no
tenant-scoped visibility and vice versa. This is the "central" half of CHECKLIST's own
"Central append-only audit query/search foundation" line - the tenant-facing one only
ever sees its own rows (RLS-enforced); this one can see every tenant's, optionally
narrowed to one via `tenant_id`.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.errors import ApiError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin/audit-events", tags=["admin-audit"])

MAX_PAGE_SIZE = 100


class AuditEventOut(BaseModel):
    id: str
    tenant_id: str | None
    actor_type: str
    actor_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    reason: str | None
    occurred_at: dt.datetime


class AuditEventPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None = None


def _encode_cursor(occurred_at: dt.datetime, event_id: uuid.UUID) -> str:
    payload = json.dumps({"t": occurred_at.isoformat(), "id": str(event_id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return dt.datetime.fromisoformat(payload["t"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ApiError(status_code=400, code="invalid_cursor", message="Pagination cursor is malformed.") from exc


@router.get("", response_model=AuditEventPage)
async def list_audit_events(
    tenant_id: uuid.UUID | None = Query(default=None, description="Narrow to one tenant; omit for all"),
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None, pattern="^(success|failure)$"),
    since: dt.datetime | None = Query(default=None),
    until: dt.datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> AuditEventPage:
    require_permission(context, "audit.read")

    filters = []
    params: dict = {"limit": limit + 1}

    if tenant_id:
        filters.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if action:
        filters.append("action = :action")
        params["action"] = action
    if target_type:
        filters.append("target_type = :target_type")
        params["target_type"] = target_type
    if outcome:
        filters.append("outcome = CAST(:outcome AS audit_outcome)")
        params["outcome"] = outcome
    if since:
        filters.append("occurred_at >= :since")
        params["since"] = since
    if until:
        filters.append("occurred_at <= :until")
        params["until"] = until
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        filters.append("(occurred_at, id) < (:cursor_time, :cursor_id)")
        params["cursor_time"] = cursor_time
        params["cursor_id"] = cursor_id

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, tenant_id, actor_type, actor_id, action, target_type, target_id,
                       outcome::text, reason, occurred_at
                FROM audit_events
                {where}
                ORDER BY occurred_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AuditEventOut(
            id=str(r[0]), tenant_id=str(r[1]) if r[1] else None, actor_type=r[2], actor_id=r[3],
            action=r[4], target_type=r[5], target_id=r[6], outcome=r[7], reason=r[8], occurred_at=r[9],
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][9], rows[-1][0]) if has_more and rows else None
    return AuditEventPage(items=items, next_cursor=next_cursor)
