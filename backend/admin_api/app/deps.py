"""FastAPI dependency wiring for the Admin API.

`current_platform_context` only accepts `csense-platform` audience tokens — a
customer-audience token is rejected here exactly as a platform token is rejected by the
Tenant API (TRD §6.1/6.2), which is the isolation property Phase 1's exit gate checks.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.config import Settings, get_settings
from csense_shared.db.postgres import platform_session
from csense_shared.errors import AuthenticationError
from csense_shared.security.tenant_context import PlatformContext
from csense_shared.security.tokens import AUDIENCE_PLATFORM, TokenError, decode_access_token


def get_app_settings() -> Settings:
    return get_settings()


async def current_platform_context(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_app_settings),
) -> PlatformContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer access token.")
    token = authorization.split(" ", 1)[1]

    try:
        claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_PLATFORM)
    except TokenError as exc:
        raise AuthenticationError("Invalid or expired access token.") from exc

    return PlatformContext(
        developer_user_id=claims.subject_user_id,
        token_audience=claims.audience,
        permissions=claims.permissions,
        correlation_id=getattr(request.state, "correlation_id", None),
    )


async def platform_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with platform_session(session_factory) as session:
        yield session
