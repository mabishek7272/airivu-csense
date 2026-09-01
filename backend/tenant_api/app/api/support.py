"""A tenant's own visibility into support grants requested against its account (SCH
§5.10) - the data behind the "AIRIVU support is currently accessing your account" banner
(TRD §7.2's own "just-in-time support grant" language), and this tenant's own right to
end one early. Row-level security already scopes every query here to this tenant's own
rows - no `tenant_id` filter needed in the SQL itself, the same as every other
tenant-owned resource this session added.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/support-grants", tags=["support"])


class SupportGrantOut(BaseModel):
    id: str
    developer_email: str
    ticket_reference: str
    purpose: str
    requested_scopes: list[str]
    status: str
    starts_at: str | None
    expires_at: str


_SELECT = """
    SELECT g.id, u.email_display, g.ticket_reference, g.purpose, g.requested_scopes,
           g.status::text, g.starts_at, g.expires_at
    FROM support_grants g JOIN users u ON u.id = g.developer_user_id
"""


def _to_out(row) -> SupportGrantOut:
    return SupportGrantOut(
        id=str(row[0]), developer_email=row[1], ticket_reference=row[2], purpose=row[3],
        requested_scopes=list(row[4] or []), status=row[5],
        starts_at=row[6].isoformat() if row[6] else None, expires_at=row[7].isoformat(),
    )


async def _expire_stale(db: AsyncSession) -> None:
    await db.execute(
        text("UPDATE support_grants SET status = 'expired', updated_at = now() "
             "WHERE status = 'active' AND expires_at < now()")
    )


@router.get("", response_model=list[SupportGrantOut])
async def list_support_grants(
    active_only: bool = Query(default=False),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[SupportGrantOut]:
    """`active_only=true` is what the CRM's own banner polls - "is anyone from support in
    our account right now", not the full history."""
    require_permission(context, "support.read")
    await _expire_stale(db)

    where = "WHERE g.status = 'active'" if active_only else ""
    rows = (await db.execute(text(f"{_SELECT} {where} ORDER BY g.created_at DESC LIMIT 200"))).all()
    return [_to_out(r) for r in rows]


class RevokeGrantIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/{grant_id}/revoke", response_model=SupportGrantOut)
async def revoke_support_grant(
    grant_id: uuid.UUID,
    body: RevokeGrantIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SupportGrantOut:
    """This tenant's own right to end an active support session early - independent of
    the platform side's own revoke (`admin_api`'s own endpoint). Row-level security
    already refuses this for a grant against a *different* tenant (the row simply isn't
    visible), so the 404 below covers both "no such grant" and "not yours"
    indistinguishably, the same discipline every other lookup in this codebase follows.
    """
    require_permission(context, "support.revoke")
    await _expire_stale(db)

    row = (await db.execute(text("SELECT status::text FROM support_grants WHERE id = :id"), {"id": grant_id})).first()
    if row is None:
        raise NotFoundError("No such support grant.")
    if row[0] != "active":
        raise ApiError(status_code=409, code="not_active", message=f"This grant is '{row[0]}', not active.")

    await db.execute(
        text(
            "UPDATE support_grants SET status = 'revoked', revoked_at = now(), "
            "revocation_reason = :reason, updated_at = now() WHERE id = :id"
        ),
        {"reason": body.reason, "id": grant_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="support_grant.revoke",
        outcome="success",
        target_type="support_grant",
        target_id=str(grant_id),
        reason=body.reason,
        before_patch={"status": "active"},
        after_patch={"status": "revoked"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="support_grant.revoked.v1",
        event_payload={"grant_id": str(grant_id), "tenant_id": str(context.tenant_id), "revoked_by": "tenant"},
        aggregate_type="support_grant",
        aggregate_id=str(grant_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE g.id = :id"), {"id": grant_id})).first()
    return _to_out(row)
