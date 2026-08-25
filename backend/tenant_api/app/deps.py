"""FastAPI dependency wiring for the Tenant API.

`current_tenant_context` is the single choke point that turns a bearer access token into
a verified `TenantContext` — no route handler ever reads `tenant_id` from a path/query
parameter for authorization purposes (TRD §7.2).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.config import Settings, get_settings
from csense_shared.db.postgres import tenant_session
from csense_shared.errors import AuthenticationError
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, TokenError, decode_access_token


def get_app_settings() -> Settings:
    return get_settings()


async def current_tenant_context(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_app_settings),
) -> TenantContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer access token.")
    token = authorization.split(" ", 1)[1]

    try:
        claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_CUSTOMER)
    except TokenError as exc:
        raise AuthenticationError("Invalid or expired access token.") from exc

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
