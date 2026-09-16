"""Cross-tenant audit trail for platform operators - `audit.read`, `platform_admin`.

Same shared permission code as the tenant-facing `GET /api/v1/tenant/audit-events`
(migration 0036) - `TenantContext`/`PlatformContext` are separate types resolved from
separate audience-scoped tokens, so a platform token holding `audit.read` carries no
tenant-scoped visibility and vice versa. This is the "central" half of CHECKLIST's own
"Central append-only audit query/search foundation" line - the tenant-facing one only
ever sees its own rows (RLS-enforced); this one can see every tenant's, optionally
narrowed to one via `tenant_id`.

**Actor display name**: see the tenant-facing endpoint's own module docstring for the
full reasoning (a single `users` join covers both `actor_type="user"` and
`"platform_developer"` - the real reason why, and the wrong-first-draft join through
`platform_developers` that a real data check caught, same text-to-text comparison to
avoid a cast error on a non-UUID `pipeline` actor id, same "`users` carries no RLS" note)
- not repeated here.
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
    actor_display_name: str | None
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

    # `ae.`-qualified throughout: the actor-name joins below add `users`/
    # `platform_developers`, both with their own `id` column, so an unqualified `id`
    # (the cursor filter's own column) would otherwise be ambiguous.
    filters = []
    params: dict = {"limit": limit + 1}

    if tenant_id:
        filters.append("ae.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if action:
        filters.append("ae.action = :action")
        params["action"] = action
    if target_type:
        filters.append("ae.target_type = :target_type")
        params["target_type"] = target_type
    if outcome:
        filters.append("ae.outcome = CAST(:outcome AS audit_outcome)")
        params["outcome"] = outcome
    if since:
        filters.append("ae.occurred_at >= :since")
        params["since"] = since
    if until:
        filters.append("ae.occurred_at <= :until")
        params["until"] = until
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        filters.append("(ae.occurred_at, ae.id) < (:cursor_time, :cursor_id)")
        params["cursor_time"] = cursor_time
        params["cursor_id"] = cursor_id

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT ae.id, ae.tenant_id, ae.actor_type, ae.actor_id,
                       actor_user.display_name AS actor_display_name,
                       ae.action, ae.target_type, ae.target_id,
                       ae.outcome::text, ae.reason, ae.occurred_at
                FROM audit_events ae
                LEFT JOIN users actor_user
                    ON ae.actor_type IN ('user', 'platform_developer')
                    AND ae.actor_id = actor_user.id::text
                {where}
                ORDER BY ae.occurred_at DESC, ae.id DESC
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
            actor_display_name=r[4], action=r[5], target_type=r[6], target_id=r[7],
            outcome=r[8], reason=r[9], occurred_at=r[10],
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][10], rows[-1][0]) if has_more and rows else None
    return AuditEventPage(items=items, next_cursor=next_cursor)
