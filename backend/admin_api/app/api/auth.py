"""Platform login (TRD §6.2: audience `csense-platform`, mandatory MFA in production).

MFA enforcement itself is a Phase 2 item (membership/step-up work); this endpoint issues
platform tokens correctly scoped today so the audience-isolation property can be proven
now, and tightens without changing the contract once MFA is wired in.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.deps import get_app_settings
from app.repositories.identity import get_platform_developer, get_platform_permissions, get_user_by_email
from csense_shared.config import Settings
from csense_shared.db.postgres import platform_session
from csense_shared.errors import AuthenticationError
from csense_shared.security.passwords import verify_password
from csense_shared.security.sessions import create_session, rotate_session
from csense_shared.security.tokens import AUDIENCE_PLATFORM, issue_access_token

# Nested under /api/v1/admin (not /api/v1/auth) so Traefik can route purely on path
# prefix without needing to inspect the request body/audience to pick a backend — the
# Tenant API owns all of /api/v1/auth/**, the Admin API owns all of /api/v1/admin/**
# (including its own auth), consistent with the gateway rule in TRD §6.3. Documented as
# a deliberate deviation from the TRD's illustrative single `/api/v1/auth/**` example in
# CLARIFICATIONS.md — this repo merges the Identity Service into each API rather than
# running it standalone (TRD §5 permits deploying logical services together).
router = APIRouter(prefix="/api/v1/admin/auth", tags=["auth"])

SESSION_COOKIE_NAME = "csense_platform_session"
REFRESH_COOKIE_NAME = "csense_platform_refresh"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _set_refresh_cookies(response: Response, *, session_id: str, refresh_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME, session_id, httponly=True, samesite="lax", secure=False, path="/api/v1/admin/auth"
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME, refresh_token, httponly=True, samesite="lax", secure=False, path="/api/v1/admin/auth"
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    session_factory: async_sessionmaker = request.app.state.session_factory

    async with platform_session(session_factory) as db:
        user = await get_user_by_email(db, body.email)
        password_hash = user.password_hash if user and user.password_hash else (
            "$argon2id$v=19$m=65536,t=3,p=2$" + "0" * 32 + "$" + "0" * 32
        )
        password_ok = verify_password(body.password, password_hash, settings)

        developer = await get_platform_developer(db, user.id) if user else None

        if user is None or not password_ok or user.status != "active" or developer is None:
            raise AuthenticationError("Invalid credentials or not a platform operator account.")

        permissions = await get_platform_permissions(db, developer.id)
        user_id = user.id

    redis_client = request.app.state.redis
    session_id, refresh_token = await create_session(
        redis_client, settings, user_id=user_id, tenant_id=None, membership_id=None, audience=AUDIENCE_PLATFORM
    )
    access_token = issue_access_token(
        settings=settings,
        user_id=user_id,
        audience=AUDIENCE_PLATFORM,
        tenant_id=None,
        membership_id=None,
        permissions=permissions,
        session_id=session_id,
    )
    _set_refresh_cookies(response, session_id=session_id, refresh_token=refresh_token)
    return AuthResponse(access_token=access_token, expires_in=settings.jwt_access_token_ttl_seconds)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    presented_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not session_id or not presented_token:
        raise AuthenticationError("Missing refresh session.")

    redis_client = request.app.state.redis
    record = await rotate_session(redis_client, settings, session_id=session_id, presented_token=presented_token)
    if record is None:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/api/v1/admin/auth")
        response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/admin/auth")
        raise AuthenticationError("Refresh session invalid, expired, or already used. Please sign in again.")

    import uuid as _uuid

    session_factory: async_sessionmaker = request.app.state.session_factory
    async with platform_session(session_factory) as db:
        developer = await get_platform_developer(db, _uuid.UUID(record["user_id"]))
        if developer is None:
            raise AuthenticationError("Platform access revoked.")
        permissions = await get_platform_permissions(db, developer.id)

    access_token = issue_access_token(
        settings=settings,
        user_id=_uuid.UUID(record["user_id"]),
        audience=AUDIENCE_PLATFORM,
        tenant_id=None,
        membership_id=None,
        permissions=permissions,
        session_id=session_id,
    )
    _set_refresh_cookies(response, session_id=session_id, refresh_token=record["new_refresh_token"])
    return AuthResponse(access_token=access_token, expires_in=settings.jwt_access_token_ttl_seconds)
