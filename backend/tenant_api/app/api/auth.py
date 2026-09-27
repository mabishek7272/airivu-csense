"""Auth endpoints (TRD §10.2): register, login, refresh, logout.

Registration and login run in `bootstrap_session()` because no tenant context exists yet
at that point — see repositories/identity.py for how each stays inside the tenant
boundary anyway (registration scopes to the tenant it just created; login uses a narrow
SECURITY DEFINER lookup). Every other endpoint in this service must use
`tenant_session()`. The Tenant API's database role has no RLS bypass available to it.
"""
from __future__ import annotations

import re
import secrets
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_app_settings
from app.repositories.identity import (
    activate_membership,
    create_organization_tenant_owner,
    get_first_active_membership,
    get_role_by_name,
    get_role_permissions,
    get_user_by_email,
)
from app.services.mobile_devices import admit_device
from app.services.sessions import create_session, revoke_session, rotate_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.db.models import Membership, Organization, Tenant, User
from csense_shared.db.postgres import bootstrap_session, set_tenant_scope, tenant_session
from csense_shared.errors import ApiError, AuthenticationError, ConflictError
from csense_shared.licensing.limits import MOBILE_DEVICE_LIMIT, WEB_SESSION_LIMIT, entitlement_limit
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.invitation_tickets import consume_invitation_ticket
from csense_shared.security.login_mfa_tickets import (
    LoginMfaTicket,
    consume_login_mfa_ticket,
    create_login_mfa_ticket,
    peek_login_mfa_ticket,
    register_failed_attempt,
)
from csense_shared.security.passwords import hash_password, verify_password
from csense_shared.security.secret_store import read_secret
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token
from csense_shared.security.totp import verify_totp

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
    # The mobile app sends `client="mobile"` plus its install id; everything else is a
    # browser. Only mobile installs count against `mobile_device.count`.
    client: str = Field(default="web", pattern="^(web|mobile)$")
    device_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    device_name: str | None = Field(default=None, max_length=120)
    platform: str | None = Field(default=None, max_length=32)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: str | None = None


class LoginOut(BaseModel):
    status: str = "authenticated"          # "authenticated" | "mfa_required"
    access_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    tenant_id: str | None = None
    mfa_ticket: str | None = None
    mfa_factor: str | None = None          # "totp" | "email_otp"


# AuthResponse stays as its own class (register/accept-invitation/refresh still return it
# directly - they never trigger MFA), but give login() a way to build a LoginOut from one:
def _authenticated(auth: "AuthResponse") -> LoginOut:
    return LoginOut(
        status="authenticated", access_token=auth.access_token, token_type=auth.token_type,
        expires_in=auth.expires_in, tenant_id=auth.tenant_id,
    )


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    return f"{base}-{uuid.uuid4().hex[:8]}"


async def _send_login_otp_email(settings: Settings, *, email: str, code: str) -> bool:
    registry = build_registry(settings)
    provider = registry.get("email")
    if provider is None:
        return False
    result = await provider.send(
        Message(
            recipient=email,
            subject="Your CSense sign-in code",
            body=(
                f"Your sign-in code is: {code}\n\n"
                "This code expires in 10 minutes. If you didn't try to sign in, you can "
                "ignore this email."
            ),
        )
    )
    return result.accepted


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
        site_scope_mode="all", site_ids=frozenset(),
    )


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str = Field(min_length=12, max_length=256)


@router.post("/accept-invitation", response_model=AuthResponse)
async def accept_invitation(
    body: AcceptInvitationRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    """The counterpart to `memberships.py`'s `invite_member` - runs pre-auth, same as
    `/register` and `/login`, since by definition there is no session yet."""
    session_factory: async_sessionmaker = request.app.state.session_factory

    ticket = await consume_invitation_ticket(request.app.state.redis, settings, body.token)
    if ticket is None:
        raise AuthenticationError("This invitation link is invalid or has expired.")

    async with bootstrap_session(session_factory) as db:
        # RLS-protected tables (memberships) need tenant scope set before they can be
        # read or written, same as create_organization_tenant_owner already does - the
        # ticket's own tenant_id is what authorizes this, not anything the caller supplied.
        await set_tenant_scope(db, ticket.tenant_id)

        membership = await db.get(Membership, ticket.membership_id)
        user = await db.get(User, ticket.user_id)
        if (
            membership is None or user is None
            or membership.tenant_id != ticket.tenant_id
            or membership.user_id != user.id
            or membership.status != "invited"
        ):
            # Covers an already-accepted, revoked, or otherwise no-longer-invited
            # membership indistinguishably from a forged ticket - none of those cases
            # should be redeemable, and the visible failure is the same either way.
            raise AuthenticationError("This invitation is no longer valid.")

        await activate_membership(
            db, membership=membership, user=user, password_hash=hash_password(body.password, settings),
        )

        await record_audit_and_outbox(
            db,
            tenant_id=ticket.tenant_id,
            actor_type="user",
            actor_id=str(user.id),
            action="membership.accept",
            outcome="success",
            target_type="membership",
            target_id=str(membership.id),
            reason="Invitation accepted",
            before_patch={"status": "invited"},
            after_patch={"status": "active"},
            correlation_id=None,
            event_type="membership.accepted.v1",
            event_payload={"membership_id": str(membership.id), "user_id": str(user.id)},
            aggregate_type="membership",
            aggregate_id=str(membership.id),
        )

        permissions = await get_role_permissions(db, membership.role_id)
        site_scope_mode = membership.site_scope_mode
        site_ids = frozenset(
            row[0] for row in (
                await db.execute(
                    text(
                        "SELECT resource_id FROM membership_resource_scopes "
                        "WHERE membership_id = :mid AND resource_type = 'site' AND effect = 'allow'"
                    ),
                    {"mid": membership.id},
                )
            ).all()
        )
        user_id, tenant_id, membership_id = user.id, membership.tenant_id, membership.id

    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
        site_scope_mode=site_scope_mode, site_ids=site_ids,
    )


@router.post("/login", response_model=LoginOut)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> LoginOut:
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

        user_id, tenant_id, membership_id = user.id, membership.tenant_id, membership.membership_id

        if body.client == "mobile" and not body.device_id:
            raise ApiError(
                status_code=422, code="device_id_required",
                message="The mobile app must identify this device to sign in.",
            )

        # MFA gate: check for an enrolled TOTP factor before touching device admission or
        # issuing anything. user_authenticators carries no RLS (unlike membership-scoped
        # tables), so this read is safe before any tenant scope is set.
        totp_row = (
            await db.execute(
                text("SELECT 1 FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = true"),
                {"uid": user_id},
            )
        ).first()
        factor = "totp" if totp_row is not None else "email_otp"

        otp_hash: str | None = None
        logger_dev_code: str | None = None
        if factor == "email_otp":
            code = f"{secrets.randbelow(1_000_000):06d}"
            otp_hash = hash_password(code, settings)
            sent = await _send_login_otp_email(settings, email=user.email_display, code=code)
            if not sent and settings.environment == "local":
                # Dev convenience only, mirroring memberships.py's invitation_link
                # fallback - never returned when an email provider is actually configured.
                logger_dev_code = code

    ticket = await create_login_mfa_ticket(
        request.app.state.redis, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, factor=factor,
        device_id=body.device_id, device_name=body.device_name, platform=body.platform,
        otp_hash=otp_hash,
    )

    if logger_dev_code is None:
        return LoginOut(
            status="mfa_required", mfa_ticket=ticket, mfa_factor=factor,
            access_token=None, expires_in=None,
        )
    return _mfa_required_with_dev_code(ticket, factor, logger_dev_code)


def _mfa_required_with_dev_code(ticket: str, factor: str, code: str) -> LoginOut:
    import logging
    logging.getLogger("tenant_api.auth").warning(
        "login_otp_email_unavailable_dev_fallback", extra={"code": code},
    )
    return LoginOut(status="mfa_required", mfa_ticket=ticket, mfa_factor=factor)


class MfaVerifyRequest(BaseModel):
    mfa_ticket: str
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    recovery_code: str | None = None


@router.post("/login/mfa-verify", response_model=LoginOut)
async def login_mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> LoginOut:
    if (body.code is None) == (body.recovery_code is None):
        raise ApiError(
            status_code=422, code="exactly_one_factor_required",
            message="Supply exactly one of code or recovery_code.",
        )

    redis_client = request.app.state.redis
    ticket = await peek_login_mfa_ticket(redis_client, settings, body.mfa_ticket)
    if ticket is None:
        raise AuthenticationError("This code has expired. Please sign in again.")

    session_factory: async_sessionmaker = request.app.state.session_factory
    used_recovery_code = False

    async with bootstrap_session(session_factory) as db:
        await set_tenant_scope(db, ticket.tenant_id)

        if ticket.factor == "totp":
            verified, used_recovery_code = await _verify_totp_or_recovery(db, settings, ticket, body)
            if not verified:
                attempts = await register_failed_attempt(redis_client, settings, body.mfa_ticket)
                if attempts >= 5:
                    raise AuthenticationError("Too many attempts. Please sign in again.")
                raise AuthenticationError("Incorrect code.")
        else:
            if body.code is None or ticket.otp_hash is None or not verify_password(body.code, ticket.otp_hash, settings):
                attempts = await register_failed_attempt(redis_client, settings, body.mfa_ticket)
                if attempts >= 5:
                    raise AuthenticationError("Too many attempts. Please sign in again.")
                raise AuthenticationError("Incorrect code.")

        membership = await get_first_active_membership(db, ticket.user_id)
        if membership is None:
            await consume_login_mfa_ticket(redis_client, settings, body.mfa_ticket)
            raise AuthenticationError("No active tenant membership for this account.")

        permissions = await get_role_permissions(db, membership.role_id)

        previous_device_session: str | None = None
        if ticket.device_id:
            previous_device_session = await admit_device(
                db, tenant_id=ticket.tenant_id, user_id=ticket.user_id, device_id=ticket.device_id,
                name=ticket.device_name, platform=ticket.platform,
                limit=await entitlement_limit(db, ticket.tenant_id, MOBILE_DEVICE_LIMIT),
            )

    await consume_login_mfa_ticket(redis_client, settings, body.mfa_ticket)

    if previous_device_session:
        await revoke_session(redis_client, settings, session_id=previous_device_session)

    auth = await _issue_tokens(
        request, response, settings,
        user_id=ticket.user_id, tenant_id=ticket.tenant_id, membership_id=ticket.membership_id,
        permissions=permissions, site_scope_mode=membership.site_scope_mode, site_ids=membership.site_ids,
        client="mobile" if ticket.device_id else "web", device_id=ticket.device_id,
    )
    return _authenticated(auth)


async def _verify_totp_or_recovery(
    db: AsyncSession, settings: Settings, ticket: "LoginMfaTicket", body: "MfaVerifyRequest"
) -> tuple[bool, bool]:
    """Returns (verified, used_recovery_code). Mirrors mfa.py's verify_mfa logic exactly -
    a TOTP-enrolled user may use either their code or a recovery code at login (owner
    confirmed this - a lost-phone user should not be locked out of the app entirely)."""
    if body.code is not None:
        row = (
            await db.execute(
                text(
                    "SELECT credential_id FROM user_authenticators "
                    "WHERE user_id = :uid AND type = 'totp' AND enabled = true"
                ),
                {"uid": ticket.user_id},
            )
        ).first()
        if row is None:
            return False, False
        keyring = keyring_from_settings(settings)
        try:
            secret = (
                await read_secret(
                    db, keyring, secret_id=uuid.UUID(row[0]), tenant_id=ticket.tenant_id,
                    purpose="customer.mfa.totp",
                )
            ).decode("ascii")
        except EnvelopeError:
            return False, False
        if not verify_totp(secret, body.code):
            return False, False
        await db.execute(
            text(
                "UPDATE user_authenticators SET last_used_at = now() "
                "WHERE user_id = :uid AND type = 'totp' AND enabled = true"
            ),
            {"uid": ticket.user_id},
        )
        return True, False

    candidates = (
        await db.execute(
            text(
                "SELECT id, secret_ciphertext FROM user_authenticators "
                "WHERE user_id = :uid AND type = 'recovery_code' AND enabled = true"
            ),
            {"uid": ticket.user_id},
        )
    ).all()
    for row_id, hashed in candidates:
        if verify_password(body.recovery_code, hashed.decode(), settings):
            await db.execute(text("DELETE FROM user_authenticators WHERE id = :id"), {"id": row_id})
            return True, True
    return False, False


async def _issue_tokens(
    request: Request,
    response: Response,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
    permissions: frozenset[str],
    site_scope_mode: str,
    site_ids: frozenset[uuid.UUID],
    client: str = "web",
    device_id: str | None = None,
) -> AuthResponse:
    redis_client = request.app.state.redis
    session_factory: async_sessionmaker = request.app.state.session_factory
    max_concurrent_web: int | None = None
    if client == "web":
        async with tenant_session(session_factory, tenant_id) as db:
            max_concurrent_web = await entitlement_limit(db, tenant_id, WEB_SESSION_LIMIT)
    session_id, refresh_token = await create_session(
        redis_client, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, audience=AUDIENCE_CUSTOMER,
        client=client, max_concurrent_web=max_concurrent_web,
    )
    if device_id:
        async with tenant_session(session_factory, tenant_id) as db:
            await db.execute(
                text("UPDATE mobile_devices SET session_id = :s WHERE device_id = :d"),
                {"s": session_id, "d": device_id},
            )
    access_token = issue_access_token(
        settings=settings,
        user_id=user_id,
        audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id,
        membership_id=membership_id,
        permissions=permissions,
        session_id=session_id,
        site_scope_mode=site_scope_mode,
        site_ids=site_ids,
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
        site_scope_mode=membership.site_scope_mode,
        site_ids=membership.site_ids,
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
