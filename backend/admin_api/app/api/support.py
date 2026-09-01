"""Just-in-time support grants (SCH §5.10, TRD §7.2: "Cross-tenant access requires an
explicit permission and, for sensitive data, a just-in-time support grant"). The
request/approve/deny/revoke lifecycle and its audit trail - not yet the authorization
half (actually elevating a real token's access using the grant). See this router's own
module docstring below for the reasoning.

**Self-approval is refused, in code, deliberately** - `support.approve` alone only
establishes "this platform developer can approve grants", not "can approve their own
request". A single person requesting and approving their own privileged access into a
customer's account would defeat the entire point of a peer-reviewed break-glass process.

**Expiry is lazy, not a scheduled job**: every read here first runs a cheap
`UPDATE ... WHERE status = 'active' AND expires_at < now()` before returning results, so
`status` in the database is never stale by more than the time until the next request -
without needing a cron job or worker this pass doesn't have anywhere to run.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin/support-grants", tags=["admin-support"])

DEFAULT_TTL_HOURS = 8
MAX_TTL_HOURS = 7 * 24

# Codes that must never appear in a support grant's requested_scopes: each one lets an
# elevated, temporary, revocable session mint something that outlives the grant itself -
# a new member (membership.manage can promote to tenant_owner), a new long-lived API
# credential (api_client.manage), a new outbound data-delivery destination
# (webhook.manage), a whole new tenant (reseller.manage_children), or the ability to end
# a *different* platform developer's own active session on the same tenant
# (support.revoke - RLS scopes that route by tenant, not by whose grant it is). A support
# session may read and act on a tenant's own operational data; it may never expand or
# manage who/what has standing access to that tenant. Confirmed against every
# customer-audience permission code with a real require_permission() call site in
# tenant_api - see this project's support-grant-authorization plan doc for the survey.
DANGEROUS_SUPPORT_SCOPES = frozenset({
    "membership.manage",
    "api_client.manage",
    "webhook.manage",
    "reseller.manage_children",
    "support.revoke",
})


def reject_dangerous_scopes(requested_scopes: list[str]) -> None:
    found = sorted(set(requested_scopes) & DANGEROUS_SUPPORT_SCOPES)
    if found:
        raise ApiError(
            status_code=422,
            code="dangerous_support_scope",
            message=(
                "This support grant cannot request "
                f"{', '.join(found)} - these permissions let an elevated session create "
                "access that would outlive the grant itself. Use a normal tenant "
                "membership or credential for anything that needs to persist."
            ),
        )


class SupportGrantOut(BaseModel):
    id: str
    developer_user_id: str
    developer_email: str
    tenant_id: str
    ticket_reference: str
    purpose: str
    requested_scopes: list[str]
    resource_scope: dict | None
    status: str
    starts_at: str | None
    expires_at: str
    approved_by: str | None
    revoked_by: str | None
    revocation_reason: str | None


_SELECT = """
    SELECT g.id, g.developer_user_id, u.email_display, g.tenant_id, g.ticket_reference,
           g.purpose, g.requested_scopes, g.resource_scope, g.status::text, g.starts_at,
           g.expires_at, g.approved_by, g.revoked_by, g.revocation_reason
    FROM support_grants g JOIN users u ON u.id = g.developer_user_id
"""


def _to_out(row) -> SupportGrantOut:
    return SupportGrantOut(
        id=str(row[0]), developer_user_id=str(row[1]), developer_email=row[2],
        tenant_id=str(row[3]), ticket_reference=row[4], purpose=row[5],
        requested_scopes=list(row[6] or []), resource_scope=row[7], status=row[8],
        starts_at=row[9].isoformat() if row[9] else None, expires_at=row[10].isoformat(),
        approved_by=str(row[11]) if row[11] else None,
        revoked_by=str(row[12]) if row[12] else None, revocation_reason=row[13],
    )


async def _expire_stale(db: AsyncSession) -> None:
    await db.execute(
        text("UPDATE support_grants SET status = 'expired', updated_at = now() "
             "WHERE status = 'active' AND expires_at < now()")
    )


@router.get("", response_model=list[SupportGrantOut])
async def list_support_grants(
    tenant_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[SupportGrantOut]:
    require_permission(context, "support.read")
    await _expire_stale(db)

    clauses = []
    params: dict = {}
    if tenant_id:
        clauses.append("g.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if status_filter:
        clauses.append("g.status = CAST(:status AS support_grant_status)")
        params["status"] = status_filter
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = (await db.execute(text(f"{_SELECT} {where} ORDER BY g.created_at DESC LIMIT 200"), params)).all()
    return [_to_out(r) for r in rows]


class RequestGrantIn(BaseModel):
    tenant_id: uuid.UUID
    ticket_reference: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=10, max_length=2000)
    requested_scopes: list[str] = Field(min_length=1)
    resource_scope: dict | None = None
    ttl_hours: int = Field(default=DEFAULT_TTL_HOURS, ge=1, le=MAX_TTL_HOURS)


@router.post("", response_model=SupportGrantOut, status_code=201)
async def request_support_grant(
    body: RequestGrantIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SupportGrantOut:
    require_permission(context, "support.request")
    reject_dangerous_scopes(body.requested_scopes)

    tenant_row = (await db.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": body.tenant_id})).first()
    if tenant_row is None:
        raise NotFoundError("No such tenant.")

    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=body.ttl_hours)
    grant_id = (
        await db.execute(
            text(
                "INSERT INTO support_grants "
                "(developer_user_id, tenant_id, ticket_reference, purpose, requested_scopes, "
                " resource_scope, expires_at) "
                "VALUES (:dev, :tenant, :ticket, :purpose, :scopes, CAST(:resource_scope AS jsonb), :expires_at) "
                "RETURNING id"
            ),
            {
                "dev": context.developer_user_id, "tenant": body.tenant_id, "ticket": body.ticket_reference,
                "purpose": body.purpose, "scopes": body.requested_scopes,
                "resource_scope": None if body.resource_scope is None else json.dumps(body.resource_scope),
                "expires_at": expires_at,
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=body.tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="support_grant.request",
        outcome="success",
        target_type="support_grant",
        target_id=str(grant_id),
        reason=f"{body.ticket_reference}: {body.purpose}",
        before_patch=None,
        after_patch={"status": "requested", "requested_scopes": body.requested_scopes},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="support_grant.requested.v1",
        event_payload={"grant_id": str(grant_id), "tenant_id": str(body.tenant_id)},
        aggregate_type="support_grant",
        aggregate_id=str(grant_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE g.id = :id"), {"id": grant_id})).first()
    return _to_out(row)


async def _load_for_transition(db: AsyncSession, grant_id: uuid.UUID) -> tuple:
    row = (
        await db.execute(
            text(
                "SELECT id, developer_user_id, tenant_id, status::text FROM support_grants WHERE id = :id"
            ),
            {"id": grant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such support grant.")
    return row


@router.post("/{grant_id}/approve", response_model=SupportGrantOut)
async def approve_support_grant(
    grant_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SupportGrantOut:
    require_permission(context, "support.approve")
    _, developer_user_id, tenant_id, status = await _load_for_transition(db, grant_id)

    if str(developer_user_id) == str(context.developer_user_id):
        raise ApiError(
            status_code=403, code="self_approval_refused",
            message="You cannot approve your own support grant request - a peer must review it.",
        )
    if status != "requested":
        raise ApiError(
            status_code=409, code="not_requestable",
            message=f"This grant is '{status}', not awaiting approval.",
        )

    await db.execute(
        text(
            "UPDATE support_grants SET status = 'active', approved_by = :by, approved_at = now(), "
            "starts_at = now(), updated_at = now() WHERE id = :id"
        ),
        {"by": context.developer_user_id, "id": grant_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="support_grant.approve",
        outcome="success",
        target_type="support_grant",
        target_id=str(grant_id),
        reason="Approved and activated",
        before_patch={"status": "requested"},
        after_patch={"status": "active"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="support_grant.approved.v1",
        event_payload={"grant_id": str(grant_id), "tenant_id": str(tenant_id)},
        aggregate_type="support_grant",
        aggregate_id=str(grant_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE g.id = :id"), {"id": grant_id})).first()
    return _to_out(row)


class DenyGrantIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/{grant_id}/deny", response_model=SupportGrantOut)
async def deny_support_grant(
    grant_id: uuid.UUID,
    body: DenyGrantIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SupportGrantOut:
    require_permission(context, "support.approve")
    _, developer_user_id, tenant_id, status = await _load_for_transition(db, grant_id)

    if str(developer_user_id) == str(context.developer_user_id):
        raise ApiError(
            status_code=403, code="self_approval_refused",
            message="You cannot deny your own support grant request - a peer must review it.",
        )
    if status != "requested":
        raise ApiError(status_code=409, code="not_requestable", message=f"This grant is '{status}', not awaiting a decision.")

    await db.execute(
        text("UPDATE support_grants SET status = 'denied', revocation_reason = :reason, updated_at = now() WHERE id = :id"),
        {"reason": body.reason, "id": grant_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="support_grant.deny",
        outcome="success",
        target_type="support_grant",
        target_id=str(grant_id),
        reason=body.reason,
        before_patch={"status": "requested"},
        after_patch={"status": "denied"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="support_grant.denied.v1",
        event_payload={"grant_id": str(grant_id), "tenant_id": str(tenant_id)},
        aggregate_type="support_grant",
        aggregate_id=str(grant_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE g.id = :id"), {"id": grant_id})).first()
    return _to_out(row)


class RevokeGrantIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/{grant_id}/revoke", response_model=SupportGrantOut)
async def revoke_support_grant(
    grant_id: uuid.UUID,
    body: RevokeGrantIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> SupportGrantOut:
    require_permission(context, "support.revoke")
    await _expire_stale(db)
    _, _developer_user_id, tenant_id, status = await _load_for_transition(db, grant_id)

    if status != "active":
        raise ApiError(status_code=409, code="not_active", message=f"This grant is '{status}', not active.")

    await db.execute(
        text(
            "UPDATE support_grants SET status = 'revoked', revoked_by = :by, revoked_at = now(), "
            "revocation_reason = :reason, updated_at = now() WHERE id = :id"
        ),
        {"by": context.developer_user_id, "reason": body.reason, "id": grant_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="support_grant.revoke",
        outcome="success",
        target_type="support_grant",
        target_id=str(grant_id),
        reason=body.reason,
        before_patch={"status": "active"},
        after_patch={"status": "revoked"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="support_grant.revoked.v1",
        event_payload={"grant_id": str(grant_id), "tenant_id": str(tenant_id), "revoked_by": "platform"},
        aggregate_type="support_grant",
        aggregate_id=str(grant_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE g.id = :id"), {"id": grant_id})).first()
    return _to_out(row)
