"""Auth endpoints (TRD §10.2): register, login, refresh, logout.

Registration and login run in `bootstrap_session()` because no tenant context exists yet
at that point — see repositories/identity.py for how each stays inside the tenant
boundary anyway (registration scopes to the tenant it just created; login uses a narrow
SECURITY DEFINER lookup). Every other endpoint in this service must use
`tenant_session()`. The Tenant API's database role has no RLS bypass available to it.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.deps import get_app_settings
from app.repositories.identity import (
    create_organization_tenant_owner,
    get_first_active_membership,
    get_role_by_name,
    get_role_permissions,
    get_user_by_email,
)
from app.services.sessions import create_session, revoke_session, rotate_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.db.models import Organization, Tenant, User
from csense_shared.db.postgres import bootstrap_session
from csense_shared.errors import ApiError, AuthenticationError, ConflictError
from csense_shared.security.passwords import hash_password, verify_password
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "csense_refresh"
SESSION_COOKIE_NAME = "csense_session"


class RegisterRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: str | None = None


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _set_refresh_cookies(response: Response, *, session_id: str, refresh_token: str) -> None:
    # Local dev: Secure=False over plain HTTP behind Traefik on localhost. Production
    # MUST set Secure=True and serve over TLS (TRD-SEC-006).
    response.set_cookie(
        SESSION_COOKIE_NAME, session_id, httponly=True, samesite="lax", secure=False, path="/api/v1/auth"
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME, refresh_token, httponly=True, samesite="lax", secure=False, path="/api/v1/auth"
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    session_factory: async_sessionmaker = request.app.state.session_factory
    correlation_id = getattr(request.state, "correlation_id", None)

    async with bootstrap_session(session_factory) as db:
        existing = await get_user_by_email(db, body.email)
        if existing is not None:
            raise ConflictError("An account with this email already exists.")

        owner_role = await get_role_by_name(db, "tenant_owner", "customer")

        organization = Organization(
            organization_type="direct_customer",
            legal_name=body.organization_name,
            display_name=body.organization_name,
            slug=_slugify(body.organization_name),
            status="active",
        )
        tenant = Tenant(status="active")
        user = User(
            email_normalized=body.email.lower(),
            email_display=body.email,
            password_hash=hash_password(body.password, settings),
            status="active",
            display_name=body.display_name,
        )

        membership = await create_organization_tenant_owner(
            db, organization=organization, tenant=tenant, user=user, owner_role=owner_role
        )

        await record_audit_and_outbox(
            db,
            tenant_id=tenant.id,
            actor_type="user",
            actor_id=str(user.id),
            action="tenant.created",
            outcome="success",
            target_type="tenant",
            target_id=str(tenant.id),
            correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
            event_type="tenant.created.v1",
            event_payload={"tenant_id": str(tenant.id), "organization_id": str(organization.id)},
            aggregate_type="tenant",
            aggregate_id=str(tenant.id),
        )

        permissions = await get_role_permissions(db, owner_role.id)
        user_id, tenant_id, membership_id = user.id, tenant.id, membership.id

    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    session_factory: async_sessionmaker = request.app.state.session_factory

    async with bootstrap_session(session_factory) as db:
        user = await get_user_by_email(db, body.email)
        # Constant-shape response whether the user exists or not, to avoid user
        # enumeration via timing/response differences (verify against a dummy hash).
        password_hash = user.password_hash if user and user.password_hash else (
            "$argon2id$v=19$m=65536,t=3,p=2$" + "0" * 32 + "$" + "0" * 32
        )
        password_ok = verify_password(body.password, password_hash, settings)

        if user is None or not password_ok or user.status != "active":
            raise AuthenticationError("Invalid email or password.")

        membership = await get_first_active_membership(db, user.id)
        if membership is None:
            raise AuthenticationError("No active tenant membership for this account.")

        permissions = await get_role_permissions(db, membership.role_id)
        user_id, tenant_id, membership_id = user.id, membership.tenant_id, membership.membership_id

    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
    )


async def _issue_tokens(
    request: Request,
    response: Response,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
    permissions: frozenset[str],
) -> AuthResponse:
    redis_client = request.app.state.redis
    session_id, refresh_token = await create_session(
        redis_client, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, audience=AUDIENCE_CUSTOMER,
    )
    access_token = issue_access_token(
        settings=settings,
        user_id=user_id,
        audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id,
        membership_id=membership_id,
        permissions=permissions,
        session_id=session_id,
    )
    _set_refresh_cookies(response, session_id=session_id, refresh_token=refresh_token)
    return AuthResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
        tenant_id=str(tenant_id),
    )


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
        response.delete_cookie(SESSION_COOKIE_NAME, path="/api/v1/auth")
        response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth")
        raise AuthenticationError("Refresh session invalid, expired, or already used. Please sign in again.")

    session_factory: async_sessionmaker = request.app.state.session_factory
    async with bootstrap_session(session_factory) as db:
        membership = await get_first_active_membership(db, uuid.UUID(record["user_id"]))
        if membership is None:
            raise AuthenticationError("No active tenant membership for this account.")
        permissions = await get_role_permissions(db, membership.role_id)

    access_token = issue_access_token(
        settings=settings,
        user_id=uuid.UUID(record["user_id"]),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=membership.tenant_id,
        membership_id=membership.membership_id,
        permissions=permissions,
        session_id=session_id,
    )
    _set_refresh_cookies(response, session_id=session_id, refresh_token=record["new_refresh_token"])
    return AuthResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
        tenant_id=str(membership.tenant_id),
    )


@router.delete("/sessions/{session_id}", status_code=204)
async def logout(session_id: str, request: Request, response: Response) -> None:
    settings = get_app_settings()
    cookie_session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_session_id and cookie_session_id != session_id:
        raise ApiError(status_code=403, code="not_authorized", message="Cannot revoke another session.")
    await revoke_session(request.app.state.redis, settings, session_id=session_id)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/api/v1/auth")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth")
