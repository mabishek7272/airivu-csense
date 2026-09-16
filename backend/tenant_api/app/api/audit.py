"""A tenant's own audit trail - read-only, `audit.read` (`tenant_owner`-only).

`audit_events` has carried real, RLS-scoped rows since migration 0001 - every feature
built this session writes through `record_audit_and_outbox`. This is the first read path
against it. Keyset-paginated the same way `incidents.py`'s own listing already is
(`(occurred_at, id)`, not offset-based - a row landing mid-scroll must not cause a skip or
a repeat).

**Actor display name**: `actor_type`/`actor_id` as recorded, joined to a friendly name
where one exists. A single join to `users` covers *both* `actor_type="user"` and
`"platform_developer"` - confirmed directly against real data before assuming otherwise:
`admin_api/app/deps.py` sets `developer_user_id=claims.subject_user_id`, the JWT's own
subject, which is a `users.id` - **not** `platform_developers.id` (a separate surrogate
key on a table that merely links `user_id` back to the same `users` row). A first draft
joined through `platform_developers` on the assumption its own `id` was what got
recorded; a real audit row's `platform_developer` actor_id matched `users.id` directly
and had no corresponding `platform_developers.id` at all, catching the mistake before it
shipped. `"pipeline"` (a system actor, not a real account) has no match, and
`actor_display_name` is `null` for it - correct, not a join failure. `users` carries no
RLS (only `memberships`/`membership_resource_scopes`/`audit_events` do - migration 0001),
so this join is not the same silent-empty-under-RLS trap the reseller child-tenant list
hit earlier this project. The join compares `actor_id` (untrusted, recorded as free text)
against `users.id` cast *to text*, not the other way around - casting `actor_id` itself
to `uuid` would throw on a non-UUID id like a `pipeline`-actor's own identifier; text-to-
text comparison never does, so this needs no `CASE WHEN` guard at all.
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

    # Every column referenced here is `ae.`-qualified: the new actor-name joins below add
    # `users`/`platform_developers`, both of which also have their own `id` column, so an
    # unqualified `id` (the cursor filter's own column) would become ambiguous otherwise.
    filters = []
    params: dict = {"limit": limit + 1}  # one extra row tells us whether more exist

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
                SELECT ae.id, ae.actor_type, ae.actor_id, actor_user.display_name AS actor_display_name,
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
            id=str(r[0]), actor_type=r[1], actor_id=r[2], actor_display_name=r[3],
            action=r[4], target_type=r[5], target_id=r[6], outcome=r[7], reason=r[8], occurred_at=r[9],
        )
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][9], rows[-1][0]) if has_more and rows else None
    return AuditEventPage(items=items, next_cursor=next_cursor)
