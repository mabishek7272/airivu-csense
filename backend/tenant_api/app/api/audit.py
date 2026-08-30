"""A tenant's own audit trail - read-only, `audit.read` (`tenant_owner`-only).

`audit_events` has carried real, RLS-scoped rows since migration 0001 - every feature
built this session writes through `record_audit_and_outbox`. This is the first read path
against it. Keyset-paginated the same way `incidents.py`'s own listing already is
(`(occurred_at, id)`, not offset-based - a row landing mid-scroll must not cause a skip or
a repeat).

**Actor display is raw** (`actor_type`/`actor_id` as recorded, not joined to a friendly
user name) - a safe conditional join is possible (Postgres's `CASE WHEN ... THEN
actor_id::uuid END` idiom avoids a cast error on a non-UUID actor id) but is a real,
separate enhancement, not done this pass.
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

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/audit-events", tags=["audit"])

MAX_PAGE_SIZE = 100


class AuditEventOut(BaseModel):
    id: str
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
    action: str | None = Query(default=None, description="Exact action, e.g. camera.manage"),
    target_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None, pattern="^(success|failure)$"),
    since: dt.datetime | None = Query(default=None),
    until: dt.datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> AuditEventPage:
    require_permission(context, "audit.read")

    filters = []
    params: dict = {"limit": limit + 1}  # one extra row tells us whether more exist

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
                SELECT id, actor_type, actor_id, action, target_type, target_id,
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
            id=str(r[0]), actor_type=r[1], actor_id=r[2], action=r[3],
            target_type=r[4], target_id=r[5], outcome=r[6], reason=r[7], occurred_at=r[8],
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][8], rows[-1][0]) if has_more and rows else None
    return AuditEventPage(items=items, next_cursor=next_cursor)
