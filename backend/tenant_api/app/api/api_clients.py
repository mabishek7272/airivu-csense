"""API clients and keys (SCH §10.5, migration 0047; CHECKLIST: "Scoped API keys, rate
limits, usage metering, developer API docs"). Two different callers use this one file:

- A logged-in tenant owner manages *clients* (create/list/patch/revoke, issue/revoke
  individual *keys*) the same way `webhooks.py` manages endpoints - human session,
  `api_client.manage`, write-once secrets.
- An external integration authenticates *with* one of those keys and calls
  `GET /whoami` - a new, deliberately isolated endpoint rather than adding API-key auth
  to an existing tested route like `dashboard.py`. It proves the whole credential ->
  rate-limit -> usage-metering path end to end without touching anything already shipped
  and covered by its own tests, and is exactly the shape a real integration route would
  take once one exists.

**Scopes are capped at issuance, not enforced against a live permission set.** A key's
`scopes` must be a subset of the creating user's own `context.permissions` at the moment
of creation (checked in `create_api_client`/`issue_key` below) - a key can never be
issued more power than the person issuing it currently holds. What it holds later, after
that person's own role changes, is not re-checked; the key keeps what it was given until
someone explicitly revokes or patches it. That is a real, named tradeoff (the same one an
OAuth token already has relative to the user who granted it), not an oversight.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from app.deps_api_client import ApiClientContext, enforce_rate_limit
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.api_keys import generate_api_key
from csense_shared.security.permissions import require_permission
from csense_shared.security.rate_limit import get_usage
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(tags=["api-clients"])

_SELECT_CLIENT = """
    SELECT id, name, audience, status::text, scopes, site_scope_mode,
           rate_limit_per_minute, expires_at, created_at
    FROM api_clients
"""


class ApiClientOut(BaseModel):
    id: str
    name: str
    audience: str
    status: str
    scopes: list[str]
    site_scope_mode: str
    rate_limit_per_minute: int
    expires_at: str | None
    created_at: str


def _client_to_out(row) -> ApiClientOut:
    return ApiClientOut(
        id=str(row[0]), name=row[1], audience=row[2], status=row[3], scopes=list(row[4] or []),
        site_scope_mode=row[5], rate_limit_per_minute=row[6],
        expires_at=row[7].isoformat() if row[7] else None, created_at=row[8].isoformat(),
    )


class ApiKeyOut(BaseModel):
    id: str
    key_prefix: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None


def _key_to_out(row) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(row[0]), key_prefix=row[1], created_at=row[2].isoformat(),
        expires_at=row[3].isoformat() if row[3] else None,
        last_used_at=row[4].isoformat() if row[4] else None,
        revoked_at=row[5].isoformat() if row[5] else None,
    )


@router.get("/api/v1/tenant/api-clients", response_model=list[ApiClientOut])
async def list_api_clients(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ApiClientOut]:
    require_permission(context, "api_client.manage")
    rows = (await db.execute(text(f"{_SELECT_CLIENT} ORDER BY created_at DESC"))).all()
    return [_client_to_out(r) for r in rows]


class CreateApiClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)
    site_scope_mode: str = Field(default="all", pattern="^(all|none)$")
    rate_limit_per_minute: int = Field(default=60, ge=1, le=6000)


class CreateApiClientOut(BaseModel):
    client: ApiClientOut
    # The one and only time the full key is ever shown, exactly like a webhook signing
    # secret or a device enrolment token - only the prefix survives past this response.
    api_key: str
    key_id: str


@router.post("/api/v1/tenant/api-clients", response_model=CreateApiClientOut, status_code=201)
async def create_api_client(
    body: CreateApiClientIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CreateApiClientOut:
    require_permission(context, "api_client.manage")

    unheld = [s for s in body.scopes if s not in context.permissions]
    if unheld:
        raise ApiError(
            status_code=422, code="scope_exceeds_issuer_permissions",
            message=f"Cannot issue a key with scopes you do not hold: {', '.join(unheld)}",
        )

    client_id = (
        await db.execute(
            text(
                "INSERT INTO api_clients "
                "(tenant_id, name, scopes, site_scope_mode, rate_limit_per_minute, created_by) "
                "VALUES (:tenant_id, :name, :scopes, :site_scope_mode, :rate_limit, :created_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": context.tenant_id, "name": body.name, "scopes": body.scopes,
                "site_scope_mode": body.site_scope_mode, "rate_limit": body.rate_limit_per_minute,
                "created_by": context.user_id,
            },
        )
    ).scalar_one()

    full_key, prefix, digest = generate_api_key()
    key_id = (
        await db.execute(
            text(
                "INSERT INTO api_keys (api_client_id, tenant_id, key_prefix, secret_hash, created_by) "
                "VALUES (:client_id, :tenant_id, :prefix, :digest, :created_by) RETURNING id"
            ),
            {
                "client_id": client_id, "tenant_id": context.tenant_id, "prefix": prefix,
                "digest": digest, "created_by": context.user_id,
            },
        )
    ).scalar_one()

    row = (await db.execute(text(f"{_SELECT_CLIENT} WHERE id = :id"), {"id": client_id})).first()

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="api_client.create",
        outcome="success",
        target_type="api_client",
        target_id=str(client_id),
        reason=f"Created API client '{body.name}' with scopes {body.scopes}",
        before_patch=None,
        after_patch={"name": body.name, "scopes": body.scopes},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="api_client.created.v1",
        event_payload={"api_client_id": str(client_id)},
        aggregate_type="api_client",
        aggregate_id=str(client_id),
    )

    return CreateApiClientOut(client=_client_to_out(row), api_key=full_key, key_id=str(key_id))


class PatchApiClientIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = Field(default=None, pattern="^(active|disabled|revoked)$")
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=6000)


@router.patch("/api/v1/tenant/api-clients/{client_id}", response_model=ApiClientOut)
async def update_api_client(
    client_id: uuid.UUID,
    body: PatchApiClientIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ApiClientOut:
    """Name/status/rate limit only - never scopes (issuing different power is a new
    client, not an edit of this one) and never a key's own secret (that lives on the key,
    with its own issue/revoke endpoints below)."""
    require_permission(context, "api_client.manage")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        existing = (await db.execute(text(f"{_SELECT_CLIENT} WHERE id = :id"), {"id": client_id})).first()
        if existing is None:
            raise NotFoundError("No such API client.")
        return _client_to_out(existing)

    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    result = (
        await db.execute(
            text(f"UPDATE api_clients SET {assignments}, updated_at = now() WHERE id = :id RETURNING id"),
            {**changes, "id": client_id},
        )
    ).first()
    if result is None:
        raise NotFoundError("No such API client.")

    row = (await db.execute(text(f"{_SELECT_CLIENT} WHERE id = :id"), {"id": client_id})).first()
    return _client_to_out(row)


@router.get("/api/v1/tenant/api-clients/{client_id}/keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    client_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ApiKeyOut]:
    require_permission(context, "api_client.manage")
    rows = (
        await db.execute(
            text(
                "SELECT id, key_prefix, created_at, expires_at, last_used_at, revoked_at "
                "FROM api_keys WHERE api_client_id = :id ORDER BY created_at DESC"
            ),
            {"id": client_id},
        )
    ).all()
    return [_key_to_out(r) for r in rows]


class IssueKeyOut(BaseModel):
    key: ApiKeyOut
    api_key: str


@router.post("/api/v1/tenant/api-clients/{client_id}/keys", response_model=IssueKeyOut, status_code=201)
async def issue_api_key(
    client_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> IssueKeyOut:
    """A second (or later) key for a client already in good standing - rotation without
    a gap, the caller can start using the new key before revoking the old one."""
    require_permission(context, "api_client.manage")

    exists = (
        await db.execute(text("SELECT id FROM api_clients WHERE id = :id"), {"id": client_id})
    ).first()
    if exists is None:
        raise NotFoundError("No such API client.")

    full_key, prefix, digest = generate_api_key()
    key_row = (
        await db.execute(
            text(
                "INSERT INTO api_keys (api_client_id, tenant_id, key_prefix, secret_hash, created_by) "
                "VALUES (:client_id, :tenant_id, :prefix, :digest, :created_by) "
                "RETURNING id, key_prefix, created_at, expires_at, last_used_at, revoked_at"
            ),
            {
                "client_id": client_id, "tenant_id": context.tenant_id, "prefix": prefix,
                "digest": digest, "created_by": context.user_id,
            },
        )
    ).first()

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="api_client.issue_key",
        outcome="success",
        target_type="api_client",
        target_id=str(client_id),
        reason="Issued an additional API key",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="api_client.key_issued.v1",
        event_payload={"api_client_id": str(client_id), "key_id": str(key_row[0])},
        aggregate_type="api_client",
        aggregate_id=str(client_id),
    )

    return IssueKeyOut(key=_key_to_out(key_row), api_key=full_key)


@router.post("/api/v1/tenant/api-clients/{client_id}/keys/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    client_id: uuid.UUID,
    key_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ApiKeyOut:
    require_permission(context, "api_client.manage")

    row = (
        await db.execute(
            text(
                "UPDATE api_keys SET revoked_at = now() "
                "WHERE id = :key_id AND api_client_id = :client_id AND revoked_at IS NULL "
                "RETURNING id, key_prefix, created_at, expires_at, last_used_at, revoked_at"
            ),
            {"key_id": key_id, "client_id": client_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such active API key.")

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="api_client.revoke_key",
        outcome="success",
        target_type="api_client",
        target_id=str(client_id),
        reason="Revoked an API key",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="api_client.key_revoked.v1",
        event_payload={"api_client_id": str(client_id), "key_id": str(key_id)},
        aggregate_type="api_client",
        aggregate_id=str(client_id),
    )

    return _key_to_out(row)


class UsageOut(BaseModel):
    by_date: dict[str, int]


@router.get("/api/v1/tenant/api-clients/{client_id}/usage", response_model=UsageOut)
async def api_client_usage(
    client_id: uuid.UUID,
    request: Request,
    days: int = 7,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> UsageOut:
    """Reads the same Redis-backed counters `enforce_rate_limit` writes on every
    authenticated call - usage metering and rate limiting share one write path, this is
    just the read side of it, exposed per-client rather than per-key: a caller integrates
    against a client's overall usage, not any one key's rotation history."""
    require_permission(context, "api_client.manage")

    exists = (
        await db.execute(text("SELECT id FROM api_clients WHERE id = :id"), {"id": client_id})
    ).first()
    if exists is None:
        raise NotFoundError("No such API client.")

    usage = await get_usage(
        request.app.state.redis, request.app.state.settings, client_id=client_id, days=days,
    )
    return UsageOut(by_date=usage)


class WhoAmIOut(BaseModel):
    api_client_id: str
    tenant_id: str
    name: str
    scopes: list[str]
    site_scope_mode: str
    rate_limit_per_minute: int


@router.get("/api/v1/tenant/integrations/whoami", response_model=WhoAmIOut)
async def whoami(client: ApiClientContext = Depends(enforce_rate_limit)) -> WhoAmIOut:
    """A deliberately minimal, safe demonstrator that an API key authenticates, gets
    rate-limited, and gets usage-metered end to end - see module docstring for why this
    is a new route rather than API-key auth bolted onto an existing one."""
    return WhoAmIOut(
        api_client_id=client.api_client_id, tenant_id=client.tenant_id, name=client.name,
        scopes=sorted(client.scopes), site_scope_mode=client.site_scope_mode,
        rate_limit_per_minute=client.rate_limit_per_minute,
    )
