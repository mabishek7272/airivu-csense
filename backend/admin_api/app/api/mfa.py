"""MFA/step-up for platform operators (TRD §6.2: "Mandatory MFA and stronger session risk
controls" / "Sensitive mutations support step-up authentication and approval policy";
TRD-SEC-010: "High-risk actions require recent MFA/step-up and create a security audit
event").

**Scope, stated plainly**: TOTP + hashed single-use recovery codes only this pass -
WebAuthn/passkeys (TRD §7.1's *preferred* option) needs a browser-side ceremony and a
relying-party config this pass doesn't build; TOTP is explicitly listed as "supported",
not merely a fallback. MFA is available and *enforced as a step-up gate on one real
high-risk mutation* (`POST /api/v1/admin/licenses` - see `licensing.py`), proving
TRD-SEC-010 against something real rather than a strawman endpoint. It is **not yet
mandatory at login for every platform session** - TRD §6.2's "mandatory" is a real,
separate, larger UX/policy decision (what happens to a platform developer who has never
enrolled - block them entirely, or force enrollment on first login) that deserves its own
pass rather than a quiet partial answer here.

The TOTP secret is stored the same way every other secret in this codebase is - through
`csense_shared.security.secret_store` (envelope-encrypted, `tenant_id=None` for a
platform-owned secret - the `encrypted_secrets` table already supports that: TRD-SEC-004's
own reasoning, applied to a secret that isn't a camera credential). Recovery codes are
hashed with the same Argon2id primitive `passwords.py` already uses for account passwords
- a recovery code *is* a password, structurally.
"""
from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, get_app_settings, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.errors import ApiError, AuthenticationError, ConflictError
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.passwords import hash_password, verify_password
from csense_shared.security.secret_store import delete_secret, read_secret, write_secret
from csense_shared.security.step_up_tickets import has_recent_step_up, mark_step_up_verified
from csense_shared.security.tenant_context import PlatformContext
from csense_shared.security.totp import generate_totp_secret, provisioning_uri, verify_totp

router = APIRouter(prefix="/api/v1/admin/auth/mfa", tags=["admin-mfa"])

TOTP_SECRET_PURPOSE = "platform.mfa.totp"
RECOVERY_CODE_COUNT = 10
STEP_UP_SCOPE = "admin-high-risk"


class StatusOut(BaseModel):
    enrolled: bool
    recovery_codes_remaining: int


@router.get("/status", response_model=StatusOut)
async def mfa_status(
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> StatusOut:
    enrolled = (
        await db.execute(
            text(
                "SELECT 1 FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = true"
            ),
            {"uid": context.developer_user_id},
        )
    ).first() is not None
    remaining = (
        await db.execute(
            text(
                "SELECT count(*) FROM user_authenticators "
                "WHERE user_id = :uid AND type = 'recovery_code' AND enabled = true"
            ),
            {"uid": context.developer_user_id},
        )
    ).scalar_one()
    return StatusOut(enrolled=enrolled, recovery_codes_remaining=remaining)


class EnrollOut(BaseModel):
    secret: str
    otpauth_uri: str


@router.post("/totp/enroll", response_model=EnrollOut, status_code=201)
async def enroll_totp(
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> EnrollOut:
    already_enrolled = (
        await db.execute(
            text("SELECT 1 FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = true"),
            {"uid": context.developer_user_id},
        )
    ).first()
    if already_enrolled is not None:
        raise ConflictError("MFA is already enrolled for this account. Remove it before re-enrolling.")

    # Clean up any abandoned, never-confirmed attempt so it doesn't linger as an orphaned
    # secret + authenticator row.
    pending = (
        await db.execute(
            text("SELECT id, credential_id FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = false"),
            {"uid": context.developer_user_id},
        )
    ).all()
    for row_id, credential_id in pending:
        if credential_id:
            await delete_secret(db, secret_id=uuid.UUID(credential_id))
        await db.execute(text("DELETE FROM user_authenticators WHERE id = :id"), {"id": row_id})

    secret = generate_totp_secret()
    keyring = keyring_from_settings(settings)
    try:
        secret_id = await write_secret(
            db, keyring, tenant_id=None, purpose=TOTP_SECRET_PURPOSE, plaintext=secret, label="Authenticator app",
        )
    except EnvelopeError as exc:
        raise ApiError(status_code=500, code="secret_store_failed", message=str(exc)) from exc

    await db.execute(
        text(
            "INSERT INTO user_authenticators (user_id, type, credential_id, label, enabled) "
            "VALUES (:uid, 'totp', :credential_id, 'Authenticator app', false)"
        ),
        {"uid": context.developer_user_id, "credential_id": str(secret_id)},
    )

    return EnrollOut(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, account_name=str(context.developer_user_id), issuer="AIRIVU CSense"),
    )


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConfirmOut(BaseModel):
    recovery_codes: list[str]


@router.post("/totp/confirm", response_model=ConfirmOut)
async def confirm_totp(
    body: ConfirmIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> ConfirmOut:
    row = (
        await db.execute(
            text(
                "SELECT id, credential_id FROM user_authenticators "
                "WHERE user_id = :uid AND type = 'totp' AND enabled = false"
            ),
            {"uid": context.developer_user_id},
        )
    ).first()
    if row is None:
        raise ApiError(
            status_code=409, code="no_pending_enrollment",
            message="No pending TOTP enrollment - call /totp/enroll first.",
        )
    authenticator_id, credential_id = row

    keyring = keyring_from_settings(settings)
    secret = (
        await read_secret(
            db, keyring, secret_id=uuid.UUID(credential_id), tenant_id=None, purpose=TOTP_SECRET_PURPOSE,
        )
    ).decode("ascii")

    if not verify_totp(secret, body.code):
        raise AuthenticationError("That code doesn't match - check your authenticator app and try again.")

    await db.execute(
        text("UPDATE user_authenticators SET enabled = true, last_used_at = now() WHERE id = :id"),
        {"id": authenticator_id},
    )

    recovery_codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(RECOVERY_CODE_COUNT)]
    for plain_code in recovery_codes:
        await db.execute(
            text(
                "INSERT INTO user_authenticators (user_id, type, secret_ciphertext, label, enabled) "
                "VALUES (:uid, 'recovery_code', :hash, 'Recovery code', true)"
            ),
            {"uid": context.developer_user_id, "hash": hash_password(plain_code, settings).encode()},
        )

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="mfa.totp.enrolled",
        outcome="success",
        target_type="user",
        target_id=str(context.developer_user_id),
        reason="TOTP enrollment confirmed",
        before_patch=None,
        after_patch={"recovery_codes_issued": RECOVERY_CODE_COUNT},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.totp.enrolled.v1",
        event_payload={"user_id": str(context.developer_user_id)},
        aggregate_type="user",
        aggregate_id=str(context.developer_user_id),
    )

    return ConfirmOut(recovery_codes=recovery_codes)


class VerifyIn(BaseModel):
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    recovery_code: str | None = None


class VerifyOut(BaseModel):
    verified: bool
    used_recovery_code: bool


@router.post("/verify", response_model=VerifyOut)
async def verify_mfa(
    body: VerifyIn,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> VerifyOut:
    if (body.code is None) == (body.recovery_code is None):
        raise ApiError(
            status_code=422, code="exactly_one_factor_required",
            message="Supply exactly one of code or recovery_code.",
        )

    used_recovery_code = False

    if body.code is not None:
        row = (
            await db.execute(
                text(
                    "SELECT credential_id FROM user_authenticators "
                    "WHERE user_id = :uid AND type = 'totp' AND enabled = true"
                ),
                {"uid": context.developer_user_id},
            )
        ).first()
        if row is None:
            raise AuthenticationError("MFA is not enrolled for this account.")
        keyring = keyring_from_settings(settings)
        secret = (
            await read_secret(
                db, keyring, secret_id=uuid.UUID(row[0]), tenant_id=None, purpose=TOTP_SECRET_PURPOSE,
            )
        ).decode("ascii")
        if not verify_totp(secret, body.code):
            raise AuthenticationError("Incorrect code.")
        await db.execute(
            text(
                "UPDATE user_authenticators SET last_used_at = now() "
                "WHERE user_id = :uid AND type = 'totp' AND enabled = true"
            ),
            {"uid": context.developer_user_id},
        )
    else:
        candidates = (
            await db.execute(
                text(
                    "SELECT id, secret_ciphertext FROM user_authenticators "
                    "WHERE user_id = :uid AND type = 'recovery_code' AND enabled = true"
                ),
                {"uid": context.developer_user_id},
            )
        ).all()
        matched_id = None
        for row_id, hashed in candidates:
            if verify_password(body.recovery_code, hashed.decode(), settings):
                matched_id = row_id
                break
        if matched_id is None:
            raise AuthenticationError("That recovery code is invalid or already used.")
        # Single-use (TRD §7.1: "recovery codes single-use") - gone the moment it's spent.
        await db.execute(text("DELETE FROM user_authenticators WHERE id = :id"), {"id": matched_id})
        used_recovery_code = True

    await mark_step_up_verified(
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.developer_user_id,
    )

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="mfa.step_up.verified",
        outcome="success",
        target_type="user",
        target_id=str(context.developer_user_id),
        reason="recovery code" if used_recovery_code else "totp code",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.step_up.verified.v1",
        event_payload={"user_id": str(context.developer_user_id), "used_recovery_code": used_recovery_code},
        aggregate_type="user",
        aggregate_id=str(context.developer_user_id),
    )

    return VerifyOut(verified=True, used_recovery_code=used_recovery_code)


@router.delete("/totp", status_code=204)
async def remove_totp(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> None:
    """Removing MFA is itself high-risk - requires a step-up verification first, not just
    the bearer token (the same reasoning `admin_api/api/licensing.py`'s gate documents)."""
    if not await has_recent_step_up(
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.developer_user_id
    ):
        raise ApiError(
            status_code=403, code="step_up_required",
            message="Removing MFA requires a recent verification - call POST /verify first.",
        )

    rows = (
        await db.execute(
            text("SELECT id, credential_id FROM user_authenticators WHERE user_id = :uid AND type = 'totp'"),
            {"uid": context.developer_user_id},
        )
    ).all()
    for row_id, credential_id in rows:
        if credential_id:
            await delete_secret(db, secret_id=uuid.UUID(credential_id))
        await db.execute(text("DELETE FROM user_authenticators WHERE id = :id"), {"id": row_id})
    await db.execute(
        text("DELETE FROM user_authenticators WHERE user_id = :uid AND type = 'recovery_code'"),
        {"uid": context.developer_user_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="mfa.totp.removed",
        outcome="success",
        target_type="user",
        target_id=str(context.developer_user_id),
        reason="MFA removed by account holder",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.totp.removed.v1",
        event_payload={"user_id": str(context.developer_user_id)},
        aggregate_type="user",
        aggregate_id=str(context.developer_user_id),
    )
