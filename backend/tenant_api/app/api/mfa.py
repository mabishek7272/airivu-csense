"""MFA/step-up for tenant users - the same TOTP + recovery-code mechanism
`admin_api/app/api/mfa.py` already ships for platform operators, mirrored here rather than
shared as a single router because the two run under genuinely different session shapes
(`TenantContext`/`db_session_for_tenant` vs `PlatformContext`/`platform_db_session` - TRD
§7.2 keeps the two services' repository code from sharing a call path).

**Scope, stated plainly**: enrollment, confirmation, verification, and removal all work
for real - a tenant member can protect their own account with a second factor today. This
pass does **not** gate any specific tenant mutation behind a step-up requirement (unlike
the Admin API side, which gates license issuance) - nothing in the Tenant API is yet
identified as "high-risk" the way TRD-SEC-010 means it; `verify` still marks a real,
checkable step-up record for whenever a tenant-side high-risk action is identified and
wired to it, rather than leaving that half of the mechanism unbuilt until then.

The TOTP secret is stored `tenant_id`-scoped this time (not `None`) - a customer's own
secret, protected by the same RLS `encrypted_secrets` already enforces for every other
tenant-owned credential (TRD-SEC-004).
"""
from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.errors import ApiError, AuthenticationError, ConflictError
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.passwords import hash_password, verify_password
from csense_shared.security.secret_store import delete_secret, read_secret, write_secret
from csense_shared.security.step_up_tickets import has_recent_step_up, mark_step_up_verified
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.totp import generate_totp_secret, provisioning_uri, verify_totp

router = APIRouter(prefix="/api/v1/tenant/auth/mfa", tags=["tenant-mfa"])

TOTP_SECRET_PURPOSE = "customer.mfa.totp"
RECOVERY_CODE_COUNT = 10
STEP_UP_SCOPE = "tenant-high-risk"


class StatusOut(BaseModel):
    enrolled: bool
    recovery_codes_remaining: int


@router.get("/status", response_model=StatusOut)
async def mfa_status(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> StatusOut:
    enrolled = (
        await db.execute(
            text("SELECT 1 FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = true"),
            {"uid": context.user_id},
        )
    ).first() is not None
    remaining = (
        await db.execute(
            text(
                "SELECT count(*) FROM user_authenticators "
                "WHERE user_id = :uid AND type = 'recovery_code' AND enabled = true"
            ),
            {"uid": context.user_id},
        )
    ).scalar_one()
    return StatusOut(enrolled=enrolled, recovery_codes_remaining=remaining)


class EnrollOut(BaseModel):
    secret: str
    otpauth_uri: str


@router.post("/totp/enroll", response_model=EnrollOut, status_code=201)
async def enroll_totp(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> EnrollOut:
    already_enrolled = (
        await db.execute(
            text("SELECT 1 FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = true"),
            {"uid": context.user_id},
        )
    ).first()
    if already_enrolled is not None:
        raise ConflictError("MFA is already enrolled for this account. Remove it before re-enrolling.")

    # Clean up any abandoned, never-confirmed attempt so it doesn't linger as an orphaned
    # secret + authenticator row.
    pending = (
        await db.execute(
            text("SELECT id, credential_id FROM user_authenticators WHERE user_id = :uid AND type = 'totp' AND enabled = false"),
            {"uid": context.user_id},
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
            db, keyring, tenant_id=context.tenant_id, purpose=TOTP_SECRET_PURPOSE,
            plaintext=secret, label="Authenticator app",
        )
    except EnvelopeError as exc:
        raise ApiError(status_code=500, code="secret_store_failed", message=str(exc)) from exc

    await db.execute(
        text(
            "INSERT INTO user_authenticators (user_id, type, credential_id, label, enabled) "
            "VALUES (:uid, 'totp', :credential_id, 'Authenticator app', false)"
        ),
        {"uid": context.user_id, "credential_id": str(secret_id)},
    )

    return EnrollOut(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, account_name=str(context.user_id), issuer="AIRIVU CSense"),
    )


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConfirmOut(BaseModel):
    recovery_codes: list[str]


@router.post("/totp/confirm", response_model=ConfirmOut)
async def confirm_totp(
    body: ConfirmIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> ConfirmOut:
    row = (
        await db.execute(
            text(
                "SELECT id, credential_id FROM user_authenticators "
                "WHERE user_id = :uid AND type = 'totp' AND enabled = false"
            ),
            {"uid": context.user_id},
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
            db, keyring, secret_id=uuid.UUID(credential_id), tenant_id=context.tenant_id, purpose=TOTP_SECRET_PURPOSE,
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
            {"uid": context.user_id, "hash": hash_password(plain_code, settings).encode()},
        )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="mfa.totp.enrolled",
        outcome="success",
        target_type="user",
        target_id=str(context.user_id),
        reason="TOTP enrollment confirmed",
        before_patch=None,
        after_patch={"recovery_codes_issued": RECOVERY_CODE_COUNT},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.totp.enrolled.v1",
        event_payload={"user_id": str(context.user_id)},
        aggregate_type="user",
        aggregate_id=str(context.user_id),
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
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
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
                {"uid": context.user_id},
            )
        ).first()
        if row is None:
            raise AuthenticationError("MFA is not enrolled for this account.")
        keyring = keyring_from_settings(settings)
        secret = (
            await read_secret(
                db, keyring, secret_id=uuid.UUID(row[0]), tenant_id=context.tenant_id, purpose=TOTP_SECRET_PURPOSE,
            )
        ).decode("ascii")
        if not verify_totp(secret, body.code):
            raise AuthenticationError("Incorrect code.")
        await db.execute(
            text(
                "UPDATE user_authenticators SET last_used_at = now() "
                "WHERE user_id = :uid AND type = 'totp' AND enabled = true"
            ),
            {"uid": context.user_id},
        )
    else:
        candidates = (
            await db.execute(
                text(
                    "SELECT id, secret_ciphertext FROM user_authenticators "
                    "WHERE user_id = :uid AND type = 'recovery_code' AND enabled = true"
                ),
                {"uid": context.user_id},
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
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.user_id,
    )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="mfa.step_up.verified",
        outcome="success",
        target_type="user",
        target_id=str(context.user_id),
        reason="recovery code" if used_recovery_code else "totp code",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.step_up.verified.v1",
        event_payload={"user_id": str(context.user_id), "used_recovery_code": used_recovery_code},
        aggregate_type="user",
        aggregate_id=str(context.user_id),
    )

    return VerifyOut(verified=True, used_recovery_code=used_recovery_code)


@router.delete("/totp", status_code=204)
async def remove_totp(
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> None:
    """Removing MFA is itself high-risk - requires a step-up verification first, not just
    the bearer token (the same reasoning the Admin API's own gate documents)."""
    if not await has_recent_step_up(
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.user_id
    ):
        raise ApiError(
            status_code=403, code="step_up_required",
            message="Removing MFA requires a recent verification - call POST /verify first.",
        )

    rows = (
        await db.execute(
            text("SELECT id, credential_id FROM user_authenticators WHERE user_id = :uid AND type = 'totp'"),
            {"uid": context.user_id},
        )
    ).all()
    for row_id, credential_id in rows:
        if credential_id:
            await delete_secret(db, secret_id=uuid.UUID(credential_id))
        await db.execute(text("DELETE FROM user_authenticators WHERE id = :id"), {"id": row_id})
    await db.execute(
        text("DELETE FROM user_authenticators WHERE user_id = :uid AND type = 'recovery_code'"),
        {"uid": context.user_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="mfa.totp.removed",
        outcome="success",
        target_type="user",
        target_id=str(context.user_id),
        reason="MFA removed by account holder",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="mfa.totp.removed.v1",
        event_payload={"user_id": str(context.user_id)},
        aggregate_type="user",
        aggregate_id=str(context.user_id),
    )
