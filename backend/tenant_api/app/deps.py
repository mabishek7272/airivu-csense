"""FastAPI dependency wiring for the Tenant API.

`current_tenant_context` is the single choke point that turns a bearer access token into
a verified `TenantContext` — no route handler ever reads `tenant_id` from a path/query
parameter for authorization purposes (TRD §7.2).

**Support-grant elevation**: a `csense-platform` audience token is also accepted here, but
only when paired with `X-CSense-Support-Tenant-Id` naming a target tenant *and* a real,
active `support_grants` row proving that developer was peer-approved for that exact tenant
(`elevate_from_grant`, checked via a narrow SECURITY DEFINER lookup — see migration 0051's
own docstring for why this can't be an ordinary RLS-scoped query). The resulting
`TenantContext.permissions` comes from the grant's own approved scopes, not the developer's
ambient platform permissions — see `csense_shared.security.support_elevation`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.config import Settings, get_settings
from csense_shared.db.postgres import bootstrap_session, tenant_session
from csense_shared.errors import AuthenticationError
from csense_shared.security.support_elevation import elevate_from_grant
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.tokens import (
    AUDIENCE_CUSTOMER,
    AUDIENCE_PLATFORM,
    TokenError,
    decode_access_token,
)


def get_app_settings() -> Settings:
    return get_settings()


async def _elevated_context_from_platform_token(
    token: str,
    tenant_id_header: str | None,
    request: Request,
    settings: Settings,
) -> TenantContext:
    if not tenant_id_header:
        raise AuthenticationError("Invalid or expired access token.")
    try:
        tenant_id = UUID(tenant_id_header)
    except ValueError as exc:
        raise AuthenticationError("Invalid or expired access token.") from exc

    try:
        claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_PLATFORM)
    except TokenError as exc:
        raise AuthenticationError("Invalid or expired access token.") from exc

    session_factory = request.app.state.session_factory
    async with bootstrap_session(session_factory) as db:
        elevated = await elevate_from_grant(
            db, developer_user_id=claims.subject_user_id, tenant_id=tenant_id
        )

    if elevated is None:
        raise AuthenticationError("Invalid or expired access token.")

    return TenantContext(
        tenant_id=tenant_id,
        user_id=claims.subject_user_id,
        membership_id=None,
        token_audience=claims.audience,
        permissions=elevated.permissions,
        support_grant_id=elevated.grant_id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )


async def current_tenant_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_support_tenant_id: str | None = Header(default=None, alias="X-CSense-Support-Tenant-Id"),
    settings: Settings = Depends(get_app_settings),
) -> TenantContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer access token.")
    token = authorization.split(" ", 1)[1]

    try:
        claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_CUSTOMER)
    except TokenError:
        # Not a valid customer token — the only other legitimate shape is a
        # support-grant-elevated platform token. Any other failure raises from there.
        return await _elevated_context_from_platform_token(token, x_support_tenant_id, request, settings)

    if claims.tenant_id is None or claims.membership_id is None:
        raise AuthenticationError("Token does not carry a tenant membership.")

    return TenantContext(
        tenant_id=claims.tenant_id,
        user_id=claims.subject_user_id,
        membership_id=claims.membership_id,
        token_audience=claims.audience,
        permissions=claims.permissions,
        correlation_id=getattr(request.state, "correlation_id", None),
    )


async def db_session_for_tenant(
    request: Request, context: TenantContext = Depends(current_tenant_context)
) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with tenant_session(session_factory, context.tenant_id) as session:
        yield session
