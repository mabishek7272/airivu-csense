# Apti Tenant Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add login-time MFA (email OTP, or TOTP if already enrolled) to both the tenant
API and web CRM, add real push notifications as a new channel in the existing
notification/escalation engine, provision a real Apti reseller organization + branding +
test tenant/camera, and turn `mobile/app` into a build-time white-labeled tenant client
shipping its first branded build for Apti — with a new 2-tab (Home/Settings) navigation,
a Home screen with Camera/Alerts/Detections, and a Settings screen with Profile/
Notifications/Logout.

**Architecture:** Backend MFA is a Redis-ticket challenge/response inserted between
password verification and token issuance in the existing `/login` endpoint, reusing the
existing TOTP/recovery-code verify logic from `mfa.py` and the existing email-provider
registry. Push is a new channel in the existing `csense_shared.notifications.dispatcher`
system (same `Recipient`/`Channel`/`ProviderRegistry` abstractions email/SMS/WhatsApp
already use), sent via Expo's hosted push relay (no APNs/FCM certs needed). Mobile
white-labeling reuses the existing `org_branding` public/authenticated endpoints and
`mobile/app`'s own already-existing `useColors()` hook seam. Every new mobile/web screen
matches this codebase's existing `{data, loading, error, refresh}` hook convention.

**Tech Stack:** FastAPI/SQLAlchemy/Alembic/Redis (backend), React/Vite (web CRM), Expo
Router/React Native (mobile), Expo push service, `expo-video`, `expo-notifications`.

---

## Scope check

This plan covers backend MFA + push (Parts 1-3), APTI provisioning (Part 4), the web CRM
MFA gate (Part 5), and the mobile white-label/nav/screens work (Part 6). These are
ordered by real dependency: Part 1 must land before Parts 5/6 can be tested end-to-end
(both clients call the new login endpoints); Part 3 (push) depends on Part 1 only for the
shared `mobile_devices` table shape, not on its logic; Part 4 can run any time after
Part 1 lands (it logs in as `client="web"` deliberately to avoid tripping the new MFA
gate during scripted provisioning, but still needs the endpoints to exist and behave).
Parts 5 and 6 are independent of each other and could be built in parallel by two
different people; this plan sequences them one after another for a single implementer.

## File Structure

New backend files: `backend/migrations/versions/0066_email_otp_authenticator_type.py`,
`backend/migrations/versions/0067_mobile_device_push_token.py`,
`backend/shared/csense_shared/security/login_mfa_tickets.py`,
`backend/tenant_api/app/api/profile.py`,
`backend/shared/csense_shared/notifications/expo_push.py`, `scripts/
provision_apti_reseller.py`. Modified: `backend/tenant_api/app/api/auth.py` (login +
new mfa-verify endpoint), `backend/shared/csense_shared/notifications/dispatcher.py`
(new `push` channel), `backend/tenant_api/app/api/devices.py` or wherever `mobile_devices`
routes live today (new push-token route), `backend/tenant_api/app/main.py` (register the
new `profile` router).

New/modified web files: `frontend/customer-crm/src/auth/AuthContext.tsx` (MFA state),
new `frontend/customer-crm/src/pages/MfaVerifyPage.tsx`, `frontend/customer-crm/src/
App.tsx` or router config (new route), `frontend/customer-crm/src/pages/LoginPage.tsx`
(handle the new discriminated response).

New/modified mobile files: `mobile/app/app.config.ts` (replaces `app.json`), `mobile/app/
eas.json` (new build profiles), new `mobile/app/brand/{apti,eaigleye,3rdi,csense}/*`
asset directories, new `mobile/app/src/theme/BrandProvider.tsx`, modified `mobile/app/src/
theme/colors.ts`, modified `mobile/app/src/auth/AuthContext.tsx` (three-way `authStage`),
new `mobile/app/src/api/auth.ts` additions (`verifyMfa`), modified `mobile/app/app/
_layout.tsx` + `app/index.tsx`, new `mobile/app/app/mfa.tsx`, restructured `mobile/app/
app/(tabs)/*` (2 tabs, nested stacks), new hooks `src/hooks/{useLiveSession,useDetections,
useProfile,useAlertsSocket}.ts`, new `src/api/tenant.ts` functions, new screens under
`app/(tabs)/home/{cameras,alerts,detections}/*` and `app/(tabs)/settings/*`.

---

# Part 1: Backend — login-time MFA

## Task 1: Migration 0066 — `email_otp` authenticator type

**Files:**
- Create: `backend/migrations/versions/0066_email_otp_authenticator_type.py`

- [ ] **Step 1: Write the migration**

```python
"""Adds 'email_otp' to authenticator_type - the login-time MFA flow (backend/tenant_api/
app/api/auth.py's new /login/mfa-verify) uses this value in audit-event payloads for a
user who has no TOTP enrolled. The challenge itself lives in Redis
(csense_shared/security/login_mfa_tickets.py), not this enum - no user_authenticators row
is ever created with this type - so this migration is not load-bearing for login to work,
but keeps audit payloads consistent with a real enum value rather than a bare string with
no schema backing anywhere.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-27
"""
from __future__ import annotations

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres cannot add an enum value inside the same transaction a later statement in
    # this same migration run might use it in - commit first, matching this repo's own
    # migration 0017 precedent for the identical situation.
    op.execute("COMMIT")
    op.execute("ALTER TYPE authenticator_type ADD VALUE IF NOT EXISTS 'email_otp'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value - same precedent as migration 0017.
    pass
```

- [ ] **Step 2: Apply it against the real dev database**

Run: `cd backend && python -m alembic upgrade head`
Expected: no errors, ends on `0066`.

- [ ] **Step 3: Confirm the value exists for real**

Run:
```bash
psql "$TEST_POSTGRES_DSN" -c "SELECT enum_range(NULL::authenticator_type);"
```
Expected: the array includes `email_otp` alongside `webauthn`, `totp`, `recovery_code`,
`oidc`.

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/versions/0066_email_otp_authenticator_type.py
git commit -m "feat(auth): add email_otp to authenticator_type enum"
```

---

## Task 2: `login_mfa_tickets.py` — the Redis challenge module (TDD)

**Files:**
- Create: `backend/shared/csense_shared/security/login_mfa_tickets.py`
- Test: `backend/tests/test_login_mfa_tickets.py`

This mirrors `invitation_tickets.py`'s Redis idiom with one deliberate difference: a
login-MFA ticket is **not** deleted on a plain read, because a user must be able to retry
a mistyped 6-digit code without the ticket vanishing on the first attempt. A companion
attempt-counter key enforces a hard cap; both keys are deleted together only once, on a
genuinely successful verify.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_login_mfa_tickets.py
"""Login-time MFA challenge tickets: single-use only on SUCCESS (unlike
invitation_tickets.py's GETDEL-on-first-read), with a bounded retry counter. Needs a real
Redis; skipped otherwise. Mirrors test_invitation_tickets.py's own shape.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.login_mfa_tickets import (
    MAX_VERIFY_ATTEMPTS,
    consume_login_mfa_ticket,
    create_login_mfa_ticket,
    peek_login_mfa_ticket,
    register_failed_attempt,
)

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TEST_REDIS_PASSWORD"), reason="TEST_REDIS_PASSWORD not set - skipping"
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture()
async def redis_client():
    client = redis.Redis(
        host="localhost", port=6379, password=os.environ["TEST_REDIS_PASSWORD"], decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        environment=f"login-mfa-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_a_valid_ticket_resolves_to_what_it_was_minted_for(redis_client, test_settings):
    user_id, tenant_id, membership_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, factor="totp",
        device_id="device-1", device_name="Pixel", platform="android", otp_hash=None,
    )

    ticket = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert ticket is not None
    assert ticket.user_id == user_id
    assert ticket.tenant_id == tenant_id
    assert ticket.membership_id == membership_id
    assert ticket.factor == "totp"
    assert ticket.device_id == "device-1"
    assert ticket.otp_hash is None


async def test_peeking_does_not_consume_the_ticket(redis_client, test_settings):
    """The whole reason this module exists instead of reusing invitation_tickets.py's
    GETDEL shape: a user must be able to retype a wrong code without losing the ticket."""
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="email_otp", device_id=None, device_name=None, platform=None, otp_hash="abc",
    )

    first = await peek_login_mfa_ticket(redis_client, test_settings, token)
    second = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert first is not None
    assert second is not None
    assert first.user_id == second.user_id


async def test_consume_deletes_the_ticket(redis_client, test_settings):
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    await consume_login_mfa_ticket(redis_client, test_settings, token)
    after = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert after is None


async def test_an_unknown_token_resolves_to_nothing(redis_client, test_settings):
    ticket = await peek_login_mfa_ticket(redis_client, test_settings, "not-a-real-token")
    assert ticket is None


async def test_failed_attempts_are_capped(redis_client, test_settings):
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    for _ in range(MAX_VERIFY_ATTEMPTS - 1):
        count = await register_failed_attempt(redis_client, test_settings, token)
        assert count < MAX_VERIFY_ATTEMPTS
        assert await peek_login_mfa_ticket(redis_client, test_settings, token) is not None

    # The attempt that reaches the cap deletes the ticket outright.
    final_count = await register_failed_attempt(redis_client, test_settings, token)
    assert final_count >= MAX_VERIFY_ATTEMPTS
    assert await peek_login_mfa_ticket(redis_client, test_settings, token) is None


async def test_a_ticket_expires_on_its_own(redis_client, test_settings, monkeypatch):
    import csense_shared.security.login_mfa_tickets as login_mfa_tickets_module

    monkeypatch.setattr(login_mfa_tickets_module, "LOGIN_MFA_TTL_SECONDS", 1)
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    ttl = await redis_client.ttl(login_mfa_tickets_module._ticket_key(test_settings, token))
    assert 0 < ttl <= 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && TEST_REDIS_PASSWORD=<your test redis password> python -m pytest tests/test_login_mfa_tickets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'csense_shared.security.login_mfa_tickets'`

- [ ] **Step 3: Write the implementation**

```python
# backend/shared/csense_shared/security/login_mfa_tickets.py
"""Login-time MFA challenge tickets: created after password verification succeeds but
before tokens are issued, consumed by POST /api/v1/auth/login/mfa-verify.

**Not single-use on read, unlike invitation_tickets.py/ws_tickets.py.** Those are
consumed the instant they're used, because their whole flow is "click a link once."
This one has a human retyping a 6-digit code, who deserves more than one try - so
`peek_login_mfa_ticket` is a plain GET, and the ticket is only ever deleted by
`consume_login_mfa_ticket` (a real success) or by `register_failed_attempt` once the
attempt cap is hit (force a fresh login rather than let a ticket be brute-forced
indefinitely within its TTL).

**Carries the mobile device fields through the gap MFA creates.** Today's `/login`
calls `admit_device()` (device/session-limit enforcement) before issuing tokens, in the
same request. Once MFA sits between password-check and token-issuance, `admit_device()`
must wait until MFA actually succeeds - a stolen password alone must not burn a device
slot or evict a legitimate session. The ticket carries `device_id`/`device_name`/
`platform` across that gap so `/login/mfa-verify` can call `admit_device()` with exactly
what `/login` would have used, unchanged.
"""
from __future__ import annotations

import json
import secrets
import uuid

import redis.asyncio as redis

from csense_shared.config import Settings

LOGIN_MFA_TTL_SECONDS = 600
MAX_VERIFY_ATTEMPTS = 5


def _ticket_key(settings: Settings, token: str) -> str:
    return f"cs:{settings.environment}:login-mfa:{token}"


def _attempts_key(settings: Settings, token: str) -> str:
    return f"cs:{settings.environment}:login-mfa-attempts:{token}"


async def create_login_mfa_ticket(
    redis_client: redis.Redis,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
    factor: str,
    device_id: str | None,
    device_name: str | None,
    platform: str | None,
    otp_hash: str | None,
) -> str:
    token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "user_id": str(user_id),
            "tenant_id": str(tenant_id),
            "membership_id": str(membership_id),
            "factor": factor,
            "device_id": device_id,
            "device_name": device_name,
            "platform": platform,
            "otp_hash": otp_hash,
        }
    )
    await redis_client.set(_ticket_key(settings, token), payload, ex=LOGIN_MFA_TTL_SECONDS)
    return token


class LoginMfaTicket:
    __slots__ = (
        "user_id", "tenant_id", "membership_id", "factor",
        "device_id", "device_name", "platform", "otp_hash",
    )

    def __init__(
        self, *, user_id: uuid.UUID, tenant_id: uuid.UUID, membership_id: uuid.UUID, factor: str,
        device_id: str | None, device_name: str | None, platform: str | None, otp_hash: str | None,
    ) -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.membership_id = membership_id
        self.factor = factor
        self.device_id = device_id
        self.device_name = device_name
        self.platform = platform
        self.otp_hash = otp_hash


def _parse(raw: str) -> LoginMfaTicket | None:
    try:
        data = json.loads(raw)
        return LoginMfaTicket(
            user_id=uuid.UUID(data["user_id"]),
            tenant_id=uuid.UUID(data["tenant_id"]),
            membership_id=uuid.UUID(data["membership_id"]),
            factor=data["factor"],
            device_id=data["device_id"],
            device_name=data["device_name"],
            platform=data["platform"],
            otp_hash=data["otp_hash"],
        )
    except (KeyError, ValueError, TypeError):
        return None


async def peek_login_mfa_ticket(
    redis_client: redis.Redis, settings: Settings, token: str
) -> LoginMfaTicket | None:
    """Plain read - does NOT consume the ticket. Returns None for a missing, expired, or
    malformed token, indistinguishably."""
    raw = await redis_client.get(_ticket_key(settings, token))
    if raw is None:
        return None
    return _parse(raw)


async def consume_login_mfa_ticket(redis_client: redis.Redis, settings: Settings, token: str) -> None:
    """Called only after a real successful verify - deletes both the ticket and its
    attempt counter, so a captured/retried token cannot be replayed for a second
    session."""
    await redis_client.delete(_ticket_key(settings, token), _attempts_key(settings, token))


async def register_failed_attempt(redis_client: redis.Redis, settings: Settings, token: str) -> int:
    """Increments the attempt counter; once it reaches MAX_VERIFY_ATTEMPTS, deletes the
    ticket outright (forces the user back to a fresh /login) rather than letting it be
    brute-forced for its whole TTL. Returns the new count."""
    key = _attempts_key(settings, token)
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, LOGIN_MFA_TTL_SECONDS)
    if count >= MAX_VERIFY_ATTEMPTS:
        await redis_client.delete(_ticket_key(settings, token), key)
    return count
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && TEST_REDIS_PASSWORD=<your test redis password> python -m pytest tests/test_login_mfa_tickets.py -v`
Expected: PASS — 6 tests

- [ ] **Step 5: Commit**

```bash
git add backend/shared/csense_shared/security/login_mfa_tickets.py backend/tests/test_login_mfa_tickets.py
git commit -m "feat(auth): add the login-time MFA challenge ticket module"
```

---

## Task 3: Modify `/login`, add `/login/mfa-verify`

**Files:**
- Modify: `backend/tenant_api/app/api/auth.py`

Current `login()` (lines ~234-280) does password/membership checks, then (for mobile)
calls `admit_device()`, then calls `_issue_tokens()` unconditionally. This task inserts
an MFA gate between the membership check and everything after it, for **both** web and
mobile clients (the owner explicitly confirmed MFA should not be mobile-only).

- [ ] **Step 1: Add the new imports and response model**

```python
# Add to the existing imports at the top of backend/tenant_api/app/api/auth.py:
import secrets

from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.login_mfa_tickets import (
    LoginMfaTicket,
    consume_login_mfa_ticket,
    create_login_mfa_ticket,
    peek_login_mfa_ticket,
    register_failed_attempt,
)
from csense_shared.security.secret_store import read_secret
from csense_shared.security.totp import verify_totp
```

```python
# Replace the existing AuthResponse class with:
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
```

- [ ] **Step 2: Add the OTP-email helper, right after `_send_invitation_email`'s
  counterpart would go (near the other private helpers, e.g. right after `_slugify`)**

```python
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
```

- [ ] **Step 3: Replace the `login()` function's body** (keep the signature and
  `response_model` change to `LoginOut`)

```python
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
        if factor == "email_otp":
            code = f"{secrets.randbelow(1_000_000):06d}"
            otp_hash = hash_password(code, settings)
            sent = await _send_login_otp_email(settings, email=user.email_display, code=code)
            if not sent and settings.environment == "local":
                # Dev convenience only, mirroring memberships.py's invitation_link
                # fallback - never returned when an email provider is actually configured.
                logger_dev_code = code
            else:
                logger_dev_code = None
        else:
            logger_dev_code = None

    ticket = await create_login_mfa_ticket(
        request.app.state.redis, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, factor=factor,
        device_id=body.device_id, device_name=body.device_name, platform=body.platform,
        otp_hash=otp_hash,
    )

    return LoginOut(
        status="mfa_required", mfa_ticket=ticket, mfa_factor=factor,
        access_token=None, expires_in=None,
        # Dev-only convenience field piggybacked onto tenant_id would be wrong - instead,
        # log the code server-side for local dev inspection rather than returning it.
    ) if logger_dev_code is None else _mfa_required_with_dev_code(ticket, factor, logger_dev_code)


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
            verified = await _verify_totp_or_recovery(db, settings, ticket, body)
            used_recovery_code = verified[1]
            if not verified[0]:
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
```

- [ ] **Step 4: Update the two remaining `AuthResponse`-returning endpoints
  (`register`, `accept_invitation`) to wrap their result the same way** — they never
  trigger MFA, but the router now advertises `LoginOut` as `/login`'s shape, so keep
  `register`/`accept_invitation`/`refresh` returning their own unchanged `AuthResponse`
  (no code change needed there — only `/login` and the new `/login/mfa-verify` use
  `LoginOut`). Confirm this by re-reading `register()`, `accept_invitation()`, and
  `refresh()` in the file: none of them call `login()` or reuse its return type, so they
  are genuinely untouched.

- [ ] **Step 5: Type-check / syntax-check**

Run: `cd backend && python -c "import ast; ast.parse(open('tenant_api/app/api/auth.py').read())"`
Expected: no output (valid syntax). Then start the real stack and confirm the service
boots: `docker compose -f infra/docker-compose.prod.yml up -d --build tenant-api` (or
your local dev equivalent), check `docker logs` for a clean startup with no import
errors.

- [ ] **Step 6: Commit**

```bash
git add backend/tenant_api/app/api/auth.py
git commit -m "feat(auth): add login-time MFA (TOTP if enrolled, else email OTP)"
```

---

## Task 4: `GET /api/v1/tenant/profile`

**Files:**
- Create: `backend/tenant_api/app/api/profile.py`
- Modify: `backend/tenant_api/app/main.py` (register the router)

- [ ] **Step 1: Write the endpoint**

```python
# backend/tenant_api/app/api/profile.py
"""The mobile Settings -> Profile screen's own data source - knowing your own name/email
isn't privileged relative to what a signed-in member already sees (branding.py's
/effective endpoint already states the same reasoning for org branding), so this needs no
extra permission check beyond a valid session.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/profile", tags=["tenant-profile"])


class ProfileOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    tenant_id: str
    organization_display_name: str


@router.get("", response_model=ProfileOut)
async def get_profile(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ProfileOut:
    row = (
        await db.execute(
            text(
                "SELECT u.email_display, u.display_name, u.locale, u.timezone, "
                "       o.display_name AS organization_display_name "
                "FROM users u "
                "JOIN tenants t ON t.id = :tenant_id "
                "JOIN organizations o ON o.id = t.organization_id "
                "WHERE u.id = :user_id"
            ),
            {"tenant_id": context.tenant_id, "user_id": context.user_id},
        )
    ).first()
    return ProfileOut(
        user_id=str(context.user_id), email=row.email_display, display_name=row.display_name,
        locale=row.locale, timezone=row.timezone, tenant_id=str(context.tenant_id),
        organization_display_name=row.organization_display_name,
    )
```

If `users` has no `locale`/`timezone` columns, check
`backend/shared/csense_shared/db/models.py`'s `User` class for the real column names
before assuming these two exist — if they don't, drop them from both the SQL and
`ProfileOut` rather than inventing columns.

- [ ] **Step 2: Register the router**

```python
# backend/tenant_api/app/main.py - add to the existing import block:
    profile,
# and to the existing app.include_router(...) calls:
app.include_router(profile.router)
```

- [ ] **Step 3: Verify against the real running stack**

```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" https://<tenant-api-host>/api/v1/tenant/profile | python3 -m json.tool
```
Expected: real `email`/`display_name`/`organization_display_name` for the logged-in user.

- [ ] **Step 4: Commit**

```bash
git add backend/tenant_api/app/api/profile.py backend/tenant_api/app/main.py
git commit -m "feat(tenant-api): add GET /api/v1/tenant/profile"
```

---

# Part 2: Backend — real push notifications

## Task 5: Migration 0067 — `mobile_devices.push_token`

**Files:**
- Create: `backend/migrations/versions/0067_mobile_device_push_token.py`

- [ ] **Step 1: Write the migration**

```python
"""Adds push_token to mobile_devices - the natural home for "this phone's current Expo
push token," since this table already represents one phone/user/tenant. Nullable: a
device that hasn't opened Notifications settings yet (or denied the OS permission) simply
has none, and push dispatch skips it.

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mobile_devices", sa.Column("push_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mobile_devices", "push_token")
```

- [ ] **Step 2: Apply and verify**

Run: `cd backend && python -m alembic upgrade head`
Then: `psql "$TEST_POSTGRES_DSN" -c "\d mobile_devices"` — confirm `push_token` appears.

- [ ] **Step 3: Commit**

```bash
git add backend/migrations/versions/0067_mobile_device_push_token.py
git commit -m "feat(mobile-devices): add push_token column"
```

---

## Task 6: `ExpoPushProvider` (TDD)

**Files:**
- Create: `backend/shared/csense_shared/notifications/expo_push.py`
- Test: `backend/tests/test_expo_push_provider.py`

Read `backend/shared/csense_shared/notifications/providers.py`'s `NotificationProvider`
Protocol and `Message`/`SendResult` shapes, and `dummy_sms.py`'s minimal provider (the
smallest existing example), before writing this — match their exact interface.

- [ ] **Step 1: Read the interface to match**

Run: `cat backend/shared/csense_shared/notifications/providers.py backend/shared/csense_shared/notifications/dummy_sms.py`

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_expo_push_provider.py
"""ExpoPushProvider: sends via Expo's hosted push relay (no APNs cert / FCM project
needed - Expo's push service is the relay for both). Uses a fake HTTP client so this
test never makes a real network call.
"""
from __future__ import annotations

import pytest

from csense_shared.notifications.expo_push import ExpoPushProvider
from csense_shared.notifications.providers import Message


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict) -> None:
        self.status_code = status_code
        self._json = json_body

    def json(self) -> dict:
        return self._json


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_request: dict | None = None

    async def post(self, url: str, *, json: dict) -> _FakeResponse:
        self.last_request = {"url": url, "json": json}
        return self._response


@pytest.mark.asyncio
async def test_a_successful_send_reports_accepted():
    http = _FakeHttpClient(_FakeResponse(200, {"data": {"status": "ok"}}))
    provider = ExpoPushProvider(http_client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[abc123]", subject="Alert", body="Something happened"))

    assert result.accepted is True
    assert http.last_request is not None
    assert http.last_request["json"]["to"] == "ExponentPushToken[abc123]"
    assert http.last_request["json"]["body"] == "Something happened"


@pytest.mark.asyncio
async def test_a_device_not_registered_error_is_reported_as_not_accepted():
    http = _FakeHttpClient(
        _FakeResponse(200, {"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})
    )
    provider = ExpoPushProvider(http_client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[stale]", subject="Alert", body="Something happened"))

    assert result.accepted is False


@pytest.mark.asyncio
async def test_an_http_error_is_reported_as_not_accepted():
    http = _FakeHttpClient(_FakeResponse(500, {}))
    provider = ExpoPushProvider(http_client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[abc]", subject="Alert", body="body"))

    assert result.accepted is False
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_expo_push_provider.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Write the implementation**

Read `providers.py`'s exact `Message`/`SendResult`/`NotificationProvider` definitions
first (their exact field names) and match them precisely — do not guess field names.

```python
# backend/shared/csense_shared/notifications/expo_push.py
"""Push notifications via Expo's hosted push service - the relay for both APNs and FCM,
so this needs no Apple push certificate or Firebase project of its own. `recipient` on
the Message is the device's Expo push token (ExponentPushToken[...]), stored on
mobile_devices.push_token (migration 0067) and resolved per-recipient by
dispatcher.py's Recipient.address_for("push").
"""
from __future__ import annotations

import logging

import httpx

from csense_shared.notifications.providers import Message, SendResult

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


class ExpoPushProvider:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

    async def send(self, message: Message) -> SendResult:
        try:
            response = await self._http.post(
                EXPO_PUSH_URL,
                json={"to": message.recipient, "title": message.subject, "body": message.body},
            )
        except httpx.HTTPError as exc:
            logger.warning("expo_push_unreachable", extra={"error": str(exc)[:200]})
            return SendResult(accepted=False)

        if response.status_code != 200:
            logger.warning("expo_push_http_error", extra={"status": response.status_code})
            return SendResult(accepted=False)

        data = response.json().get("data", {})
        if data.get("status") != "ok":
            # DeviceNotRegistered is the case the caller should clear the stored token
            # for - logged here so it's visible, cleared by the caller (dispatcher.py's
            # push-channel resolution), since this provider has no DB access of its own.
            logger.info("expo_push_ticket_error", extra={"detail": data.get("details")})
            return SendResult(accepted=False)

        return SendResult(accepted=True)
```

If `SendResult`'s real constructor takes different/more fields than just `accepted`
(check `providers.py`), match its real shape exactly rather than the simplified version
above.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && python -m pytest tests/test_expo_push_provider.py -v`
Expected: PASS — 3 tests

- [ ] **Step 6: Register the provider** in `csense_shared/notifications/bootstrap.py`'s
  `build_registry()` — read that function first, then add push alongside the existing
  email/SMS/WhatsApp registrations following its exact existing pattern (likely
  `registry.register("push", ExpoPushProvider())`, but match whatever the real
  registration call looks like).

- [ ] **Step 7: Commit**

```bash
git add backend/shared/csense_shared/notifications/expo_push.py backend/tests/test_expo_push_provider.py
git commit -m "feat(notifications): add ExpoPushProvider for the new push channel"
```

---

## Task 7: Push-token registration endpoint + `Recipient.address_for("push")`

**Files:**
- Modify: whichever file already owns `mobile_devices` routes today (find it: `grep -rln
  "mobile_devices" backend/tenant_api/app/api/*.py`) — add the push-token route there,
  not a new file, to keep device-related routes together.
- Modify: `backend/shared/csense_shared/notifications/dispatcher.py`

- [ ] **Step 1: Add the endpoint** to the file found above, following its existing
  session/permission pattern for a device the caller owns:

```python
class PushTokenIn(BaseModel):
    push_token: str = Field(min_length=1, max_length=400)


@router.post("/{device_id}/push-token", status_code=204)
async def set_push_token(
    device_id: str,
    body: PushTokenIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    result = await db.execute(
        text(
            "UPDATE mobile_devices SET push_token = :token "
            "WHERE tenant_id = :tenant_id AND user_id = :user_id AND device_id = :device_id"
        ),
        {"token": body.push_token, "tenant_id": context.tenant_id, "user_id": context.user_id, "device_id": device_id},
    )
    if result.rowcount == 0:
        raise NotFoundError("No such device for this account.")
```
Adjust the import list and router variable name to match whatever the existing file
already uses — this is meant to slot into that file's existing conventions, not
introduce new ones.

- [ ] **Step 2: Extend `Recipient.address_for`** in `dispatcher.py` — read the existing
  method first (its current channel branches, e.g. `email`/`sms`/`whatsapp`) and add a
  `"push"` branch resolving via a join from the recipient's `user_id` to
  `mobile_devices.push_token` (a user may have more than one registered device — this
  should be able to return multiple addresses if the existing method's return shape
  supports a list, or the caller should be prepared to fan out to each one; match
  whatever shape `address_for` already returns for other channels rather than inventing
  a new return type just for push).

- [ ] **Step 3: Verify against the real running stack**

```bash
curl -s -X POST -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"push_token": "ExponentPushToken[test123]"}' \
  https://<tenant-api-host>/api/v1/tenant/devices/<device-id>/push-token -i
```
Expected: `204`. Then: `psql "$TEST_POSTGRES_DSN" -c "SELECT push_token FROM mobile_devices WHERE device_id = '<device-id>';"` — confirms the real column was set.

- [ ] **Step 4: Commit**

```bash
git add backend/tenant_api/app/api/<the-devices-file>.py backend/shared/csense_shared/notifications/dispatcher.py
git commit -m "feat(push): add device push-token registration and push channel resolution"
```

---

# Part 3: Backend — APTI provisioning

## Task 8: `scripts/provision_apti_reseller.py`

**Files:**
- Create: `scripts/provision_apti_reseller.py`

This drives the platform's real HTTP APIs end-to-end — no raw SQL. Read
`scripts/seed_demo_tenant.py` first for this repo's existing script conventions
(argument parsing, base-URL config, printing generated credentials at the end).

- [ ] **Step 1: Read the existing seed script's conventions**

Run: `cat scripts/seed_demo_tenant.py`

- [ ] **Step 2: Write the script**

```python
#!/usr/bin/env python3
"""Provisions a real Apti reseller organization + branding + one test tenant with a site
and camera, driving the platform's real HTTP APIs end-to-end (never raw SQL - every step
below already has a real, tested endpoint). Prints every generated credential/id at the
end, matching seed_demo_tenant.py's own convention.

Usage:
  python scripts/provision_apti_reseller.py \
    --admin-api-base https://<admin-api-host> \
    --tenant-api-base https://<tenant-api-host> \
    --platform-admin-email admin@platform.dev \
    --platform-admin-password <the real seeded password> \
    --camera-site-name "Apti HQ" --camera-site-code apti-hq \
    --camera-name "Front door" --camera-code front-door \
    --camera-hostname <real reachable hostname/ip> --camera-username <real username> \
    --camera-password <real password> [--camera-rtsp-port 554]

The --camera-* arguments describe a REAL, reachable Apti camera (the owner confirmed
real hardware is available) - this script does not fabricate a working camera, it
registers whatever real one you point it at.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx


def _post(client: httpx.Client, path: str, body: dict, *, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(path, json=body, headers=headers)
    response.raise_for_status()
    return response.json() if response.content else {}


def _put(client: httpx.Client, path: str, body: dict, *, token: str) -> dict:
    response = client.put(path, json=body, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json() if response.content else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin-api-base", required=True)
    parser.add_argument("--tenant-api-base", required=True)
    parser.add_argument("--platform-admin-email", required=True)
    parser.add_argument("--platform-admin-password", required=True)
    parser.add_argument("--camera-site-name", default="Apti HQ")
    parser.add_argument("--camera-site-code", default="apti-hq")
    parser.add_argument("--camera-name", default="Test camera")
    parser.add_argument("--camera-code", default="test-camera")
    parser.add_argument("--camera-hostname", required=True)
    parser.add_argument("--camera-username", required=True)
    parser.add_argument("--camera-password", required=True)
    parser.add_argument("--camera-rtsp-port", type=int, default=554)
    args = parser.parse_args()

    admin = httpx.Client(base_url=args.admin_api_base, timeout=30.0)
    tenant = httpx.Client(base_url=args.tenant_api_base, timeout=30.0)

    print("1. Logging in as platform admin...")
    login = _post(admin, "/api/v1/admin/auth/login", {
        "email": args.platform_admin_email, "password": args.platform_admin_password,
    })
    admin_token = login["access_token"]

    print("2. Creating the Apti reseller organization...")
    org = _post(admin, "/api/v1/admin/organizations", {
        "organization_name": "Apti",
        "organization_type": "reseller",
        "owner_email": "owner@apti.example",
        "owner_display_name": "Apti Owner",
    }, token=admin_token)
    print(json.dumps(org, indent=2))

    if org.get("invitation_link"):
        print("3. Accepting Apti owner's invitation via the printed link...")
        # Extract the token from the invitation_link's query string / path - the real
        # link shape depends on FRONTEND_BASE_URL config; parse it rather than assume.
        import urllib.parse
        parsed = urllib.parse.urlparse(org["invitation_link"])
        token = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
        if token is None:
            print("Could not parse invitation token from link - accept it manually:", org["invitation_link"])
            sys.exit(1)
    else:
        print("Invitation email was sent to owner@apti.example - accept it manually and re-run "
              "from step 4 with --apti-owner-token, OR check your dev email provider's log for the link.")
        sys.exit(0)

    apti_owner_password = "ApTi-Owner-" + org["organization_id"][:8]
    accepted = _post(tenant, "/api/v1/auth/accept-invitation", {"token": token, "password": apti_owner_password})

    print("4. Setting Apti's branding...")
    branding = _put(admin, f"/api/v1/admin/organizations/{org['organization_id']}/branding", {
        "slug": "apti",
        "display_name": "Apti",
        "email_from_name": "Apti Security",
        "color_primary": "#5d5294",
        "color_accent": "#5c5783",
        "color_canvas": "#f3f5fe",
    }, token=admin_token)
    print(json.dumps(branding, indent=2))

    print("5. Logging in as Apti owner (client=web, deliberately, to skip the MFA gate "
          "during scripted provisioning)...")
    apti_login = _post(tenant, "/api/v1/auth/login", {
        "email": "owner@apti.example", "password": apti_owner_password, "client": "web",
    })
    apti_token = apti_login["access_token"]

    print("6. Creating a real test tenant under Apti (a reseller_customer)...")
    child = _post(tenant, "/api/v1/tenant/child-tenants", {
        "organization_name": "Apti Test Customer",
        "owner_email": "owner@apti-test-customer.example",
        "owner_display_name": "Test Customer Owner",
    }, token=apti_token)
    print(json.dumps(child, indent=2))

    print("Provisioning of the org/branding/test-tenant shell is done. The test tenant's "
          "own owner must now accept their invitation before sites/cameras can be "
          "created under it - accept it (see the printed invitation link/email above), "
          "then re-run this script with --skip-to-camera-setup and the real owner "
          "credentials to add the site/camera/second-TOTP-user in one pass.")


if __name__ == "__main__":
    main()
```

This script is deliberately split into two runs (org/branding/test-tenant shell, then
site/camera/second-user) because invitation acceptance is a real out-of-band step
(checking an inbox) that can't be scripted away — match `seed_demo_tenant.py`'s own
handling of this exact situation if it already solves it more completely; if so, prefer
reusing that pattern instead of the two-run split above.

- [ ] **Step 2: Run it for real against the running stack**

```bash
python scripts/provision_apti_reseller.py \
  --admin-api-base https://<admin-api-host> --tenant-api-base https://<tenant-api-host> \
  --platform-admin-email admin@platform.dev --platform-admin-password <real password> \
  --camera-hostname <real Apti camera host> --camera-username <real user> --camera-password <real password>
```
Expected: real organization/branding rows created; confirm via
`curl -s https://<tenant-api-host>/api/v1/public/branding/apti` returning Apti's real
branding.

- [ ] **Step 3: Commit**

```bash
git add scripts/provision_apti_reseller.py
git commit -m "feat(provisioning): add script to provision the Apti reseller org/branding/test tenant"
```

---

# Part 4: Web CRM — the same MFA gate

## Task 9: `AuthContext.tsx` MFA state + `MfaVerifyPage.tsx`

**Files:**
- Modify: `frontend/customer-crm/src/auth/AuthContext.tsx`
- Modify: `frontend/customer-crm/src/pages/LoginPage.tsx`
- Create: `frontend/customer-crm/src/pages/MfaVerifyPage.tsx`
- Modify: wherever routes are declared (find it: `grep -rln "LoginPage" frontend/customer-crm/src`)

- [ ] **Step 1: Update `AuthContext.tsx`**

```typescript
// Replace the AuthResponse interface and AuthState interface with:
interface LoginResult {
  status: "authenticated" | "mfa_required";
  access_token?: string;
  expires_in?: number;
  tenant_id?: string | null;
  mfa_ticket?: string;
  mfa_factor?: "totp" | "email_otp";
}

interface MfaChallenge {
  ticket: string;
  factor: "totp" | "email_otp";
}

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  tenantId: string | null;
  mfaChallenge: MfaChallenge | null;
  login: (email: string, password: string) => Promise<void>;
  verifyMfa: (input: { code?: string; recovery_code?: string }) => Promise<void>;
  cancelMfa: () => void;
  register: (organizationName: string, email: string, password: string, displayName: string) => Promise<void>;
  acceptInvitation: (token: string, password: string) => Promise<void>;
  logout: () => void;
}
```

```typescript
// Replace the login() implementation:
const [mfaChallenge, setMfaChallenge] = useState<MfaChallenge | null>(null);

const login = useCallback(async (email: string, password: string) => {
  const data = await apiFetch<LoginResult>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  if (data.status === "mfa_required") {
    setMfaChallenge({ ticket: data.mfa_ticket!, factor: data.mfa_factor! });
    return;
  }
  setAccessToken(data.access_token!, data.expires_in!);
  setTenantId(data.tenant_id ?? null);
  setIsAuthenticated(true);
}, []);

const verifyMfa = useCallback(async (input: { code?: string; recovery_code?: string }) => {
  if (!mfaChallenge) throw new Error("No MFA challenge in progress.");
  const data = await apiFetch<LoginResult>("/api/v1/auth/login/mfa-verify", {
    method: "POST",
    body: JSON.stringify({ mfa_ticket: mfaChallenge.ticket, ...input }),
  });
  setAccessToken(data.access_token!, data.expires_in!);
  setTenantId(data.tenant_id ?? null);
  setIsAuthenticated(true);
  setMfaChallenge(null);
}, [mfaChallenge]);

const cancelMfa = useCallback(() => {
  setMfaChallenge(null);
}, []);
```

Add `mfaChallenge`, `verifyMfa`, `cancelMfa` to the `<AuthContext.Provider value={{...}}>`
object. `register`/`acceptInvitation`/`logout` are unchanged (never return `mfa_required`).

- [ ] **Step 2: Update `LoginPage.tsx`** — after a successful `login()` call, check
  `useAuth().mfaChallenge`; if set, navigate/redirect to the MFA verify route instead of
  the dashboard (read the file's existing post-login navigation to match its exact
  routing mechanism — React Router `navigate()` or similar).

- [ ] **Step 3: Write `MfaVerifyPage.tsx`**

```typescript
// frontend/customer-crm/src/pages/MfaVerifyPage.tsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function MfaVerifyPage() {
  const { mfaChallenge, verifyMfa, cancelMfa } = useAuth();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [useRecoveryCode, setUseRecoveryCode] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!mfaChallenge) {
    navigate("/login");
    return null;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await verifyMfa(useRecoveryCode ? { recovery_code: recoveryCode } : { code });
      navigate("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Incorrect code.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <h1>Enter your code</h1>
      <p>
        {mfaChallenge.factor === "totp"
          ? "Enter the 6-digit code from your authenticator app."
          : "Check your email for a 6-digit sign-in code."}
      </p>
      {error && <div className="error">{error}</div>}
      <form onSubmit={submit}>
        {!useRecoveryCode ? (
          <input value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} placeholder="123456" />
        ) : (
          <input value={recoveryCode} onChange={(e) => setRecoveryCode(e.target.value)} placeholder="recovery code" />
        )}
        <button type="submit" disabled={busy}>{busy ? "Verifying…" : "Verify"}</button>
      </form>
      {mfaChallenge.factor === "totp" && (
        <button type="button" onClick={() => setUseRecoveryCode((v) => !v)}>
          {useRecoveryCode ? "Use my authenticator code instead" : "Use a recovery code instead"}
        </button>
      )}
      <button type="button" onClick={() => { cancelMfa(); navigate("/login"); }}>Start over</button>
    </div>
  );
}
```

Match the file's real CSS class names/component library conventions (check an existing
page like `LoginPage.tsx` for the actual styling approach before assuming plain
`className` strings are right).

- [ ] **Step 4: Add the route** wherever routes are declared, e.g.:

```typescript
<Route path="/mfa-verify" element={<MfaVerifyPage />} />
```

- [ ] **Step 5: Verify manually** — start the dev server, log in as a real user with
  TOTP enrolled, confirm the MFA page appears and a correct code logs in; log in as a
  user with no TOTP, confirm a real email arrives and the code works; try an incorrect
  code and confirm the error shows without crashing.

- [ ] **Step 6: Commit**

```bash
git add frontend/customer-crm/src/auth/AuthContext.tsx frontend/customer-crm/src/pages/LoginPage.tsx frontend/customer-crm/src/pages/MfaVerifyPage.tsx
git commit -m "feat(auth): add the login-time MFA verification step to the web CRM"
```

---

# Part 5: Mobile — white-label branding + navigation

## Task 10: `app.config.ts` + per-brand build profiles

**Files:**
- Create: `mobile/app/app.config.ts` (reads and replaces `mobile/app/app.json`)
- Modify: `mobile/app/eas.json`
- Create: `mobile/app/brand/{apti,eaigleye,3rdi,csense}/` (asset directories)

- [ ] **Step 1: Read the existing `app.json` and `mobile/apti/eas.json`/`app.json`**
  (the established precedent for per-brand app config in this repo) before writing
  `app.config.ts` — convert every static field `app.json` had into the equivalent
  `app.config.ts` export, then add the brand-variant switch.

```typescript
// mobile/app/app.config.ts
import type { ExpoConfig } from 'expo/config';

const VARIANT = process.env.APP_VARIANT ?? 'csense';

const BRAND_NAMES: Record<string, string> = {
  apti: 'Apti', eaigleye: 'eAIgleye', '3rdi': '3RDi', csense: 'CSense',
};

const BRAND_IDS: Record<string, string> = {
  apti: 'ai.airivu.apti.client', eaigleye: 'ai.airivu.eaigleye', '3rdi': 'ai.airivu.threerdi', csense: 'ai.airivu.app',
};

export default (): ExpoConfig => ({
  name: BRAND_NAMES[VARIANT] ?? 'CSense',
  slug: 'csense-tenant-app',
  scheme: `csense-${VARIANT}`,
  version: '1.0.0',
  orientation: 'portrait',
  userInterfaceStyle: 'light',
  icon: `./brand/${VARIANT}/icon.png`,
  ios: { supportsTablet: false, bundleIdentifier: BRAND_IDS[VARIANT] ?? BRAND_IDS.csense },
  android: {
    package: BRAND_IDS[VARIANT] ?? BRAND_IDS.csense,
    adaptiveIcon: {
      foregroundImage: `./brand/${VARIANT}/android-icon-foreground.png`,
      backgroundColor: '#f3f5fe',
    },
  },
  plugins: [
    ['expo-splash-screen', { image: `./brand/${VARIANT}/splash-icon.png`, backgroundColor: '#f3f5fe' }],
  ],
  extra: { brandSlug: VARIANT },
});
```

Adjust any fields the real existing `app.json` had that aren't reflected above (e.g.
`expo-router`'s own plugin entry, `expo-secure-store` config, cookie-manager config) —
carry every existing field over exactly, only adding the variant-driven parts.

- [ ] **Step 2: Populate `brand/apti/`** with real assets — reuse
  `mobile/apti/assets/apti-logo.png` and the already-generated icon/splash variants from
  that app (`icon.png`, `android-icon-foreground.png`, `splash-icon.png`, `favicon.png`)
  by copying them into `mobile/app/brand/apti/` with the same filenames this config
  references.

- [ ] **Step 3: Stub `brand/{eaigleye,3rdi,csense}/`** with copies of whatever the
  current default CSense assets are (from the existing `mobile/app/assets/` directory),
  plus a `# TODO: real <brand> assets` comment in a sibling `README.md` in each directory.

- [ ] **Step 4: Add EAS build profiles** to `mobile/app/eas.json`, modeled on `mobile/
  apti/eas.json`'s existing profile shape:

```json
{
  "build": {
    "apti-preview": {
      "distribution": "internal",
      "android": { "buildType": "apk" },
      "env": { "APP_VARIANT": "apti" }
    }
  }
}
```
(merge into the existing `eas.json` rather than replacing it — keep whatever profiles
already exist there.)

- [ ] **Step 5: Verify the config evaluates**

Run: `cd mobile/app && APP_VARIANT=apti npx expo config --type public | head -30`
Expected: real JSON showing `name: "Apti"`, the apti bundle id, and `extra.brandSlug: "apti"`.

- [ ] **Step 6: Commit**

```bash
git add mobile/app/app.config.ts mobile/app/eas.json mobile/app/brand
git rm mobile/app/app.json
git commit -m "feat(mobile): convert to app.config.ts with per-brand build variants"
```

---

## Task 11: `BrandProvider` + dynamic `useColors()`

**Files:**
- Create: `mobile/app/src/theme/BrandProvider.tsx`
- Modify: `mobile/app/src/theme/colors.ts`
- Modify: `mobile/app/app/_layout.tsx` (wrap with `BrandProvider`)

- [ ] **Step 1: Add `fetchPublicBranding`/`fetchEffectiveBranding` to
  `src/api/tenant.ts`** (or a new small file, following whatever pattern the existing
  domain-file convention uses):

```typescript
export interface PublicBranding {
  slug: string;
  display_name: string;
  logo_url: string | null;
  favicon_url: string | null;
  color_primary: string | null;
  color_accent: string | null;
  color_canvas: string | null;
}

export async function fetchPublicBranding(slug: string): Promise<PublicBranding> {
  return apiRequest<PublicBranding>(`/api/v1/public/branding/${slug}`, { auth: false });
}

export async function fetchEffectiveBranding(): Promise<PublicBranding | null> {
  return apiRequest<PublicBranding | null>('/api/v1/tenant/branding/effective');
}
```

- [ ] **Step 2: Write `BrandProvider.tsx`**

```typescript
// mobile/app/src/theme/BrandProvider.tsx
import Constants from 'expo-constants';
import React, { createContext, useContext, useEffect, useState } from 'react';
import { fetchEffectiveBranding, fetchPublicBranding } from '../api/tenant';
import { palette, type Palette } from './colors';
import { useAuth } from '../auth/AuthContext';

interface BrandState {
  colors: Palette;
  displayName: string;
  logoUrl: string | null;
}

const BrandContext = createContext<BrandState>({ colors: palette, displayName: 'CSense', logoUrl: null });

function withOverrides(base: Palette, primary?: string | null, accent?: string | null, canvas?: string | null): Palette {
  return { ...base, accent: primary ?? base.accent, accentHover: accent ?? base.accentHover, bg: canvas ?? base.bg };
}

export function BrandProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<BrandState>({ colors: palette, displayName: 'CSense', logoUrl: null });
  const { authStage } = useAuth();

  useEffect(() => {
    const slug = (Constants.expoConfig?.extra as { brandSlug?: string } | undefined)?.brandSlug ?? 'csense';
    fetchPublicBranding(slug)
      .then((b) =>
        setState({
          colors: withOverrides(palette, b.color_primary, b.color_accent, b.color_canvas),
          displayName: b.display_name,
          logoUrl: b.logo_url,
        }),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (authStage !== 'loggedIn') return;
    fetchEffectiveBranding()
      .then((b) => {
        if (!b) return;
        setState({
          colors: withOverrides(palette, b.color_primary, b.color_accent, b.color_canvas),
          displayName: b.display_name,
          logoUrl: b.logo_url,
        });
      })
      .catch(() => {});
  }, [authStage]);

  return <BrandContext.Provider value={state}>{children}</BrandContext.Provider>;
}

export function useBrand(): BrandState {
  return useContext(BrandContext);
}
```

- [ ] **Step 3: Modify `colors.ts`** — rename the existing `const palette = {...}` to
  `export const palette` (so `BrandProvider` can import the fallback), and change
  `useColors()`'s body from returning the const directly to `return useBrand().colors;`
  (import `useBrand` from the new file). Every existing call site (`const colors =
  useColors()`) needs zero changes.

- [ ] **Step 4: Wrap `BrandProvider` around the app** in `app/_layout.tsx` — it must sit
  inside `AuthProvider` is wrong (it reads `useAuth()`), so it must be a **child** of
  `AuthProvider`, not a parent:

```typescript
// app/_layout.tsx - change:
<AuthProvider><RootLayoutNav /></AuthProvider>
// to:
<AuthProvider><BrandProvider><RootLayoutNav /></BrandProvider></AuthProvider>
```

- [ ] **Step 5: Verify manually** — run the app with `APP_VARIANT=apti`, confirm the
  splash/login screen renders colors fetched from a real `GET /api/v1/public/branding/apti`
  call (check via a temporary console.log or React DevTools that `state.colors.accent`
  actually changed from the static default).

- [ ] **Step 6: Commit**

```bash
git add mobile/app/src/theme/BrandProvider.tsx mobile/app/src/theme/colors.ts mobile/app/app/_layout.tsx mobile/app/src/api/tenant.ts
git commit -m "feat(mobile): add dynamic per-brand theming via BrandProvider"
```

---

## Task 12: `AuthContext` three-way `authStage` + MFA screen

**Files:**
- Modify: `mobile/app/src/auth/AuthContext.tsx`
- Modify: `mobile/app/src/api/auth.ts`
- Create: `mobile/app/app/mfa.tsx`
- Modify: `mobile/app/app/_layout.tsx`
- Modify: `mobile/app/app/index.tsx`
- Modify: `mobile/app/__tests__/AuthContext.test.tsx` (the existing test file — read it
  first, it will need real updates for the new state shape, not just passing by accident)

- [ ] **Step 1: Read the existing `AuthContext.test.tsx`** to understand exactly what it
  currently asserts, so the rewrite doesn't silently drop coverage.

Run: `cat mobile/app/__tests__/AuthContext.test.tsx`

- [ ] **Step 2: Update `src/api/auth.ts`** — add the discriminated response type and a
  new `verifyMfa` function:

```typescript
export interface LoginResult {
  status: 'authenticated' | 'mfa_required';
  access_token?: string;
  expires_in?: number;
  tenant_id?: string | null;
  mfa_ticket?: string;
  mfa_factor?: 'totp' | 'email_otp';
}

// Change login()'s return type from AuthResponse to LoginResult, and change its final
// lines from unconditionally calling tokenStore.setSession(...) to:
export async function login(email: string, password: string): Promise<LoginResult> {
  const result = await apiRequest<LoginResult>('/api/v1/auth/login', {
    method: 'POST', auth: false,
    body: JSON.stringify({
      email, password, client: 'mobile',
      device_id: await getOrCreateDeviceId(), device_name: getDeviceName(), platform: Platform.OS,
    }),
  });
  if (result.status === 'authenticated') {
    await tokenStore.setSession({
      access_token: result.access_token!, expires_in: result.expires_in!, tenant_id: result.tenant_id ?? null,
    });
  }
  return result;
}

export async function verifyMfa(input: { mfa_ticket: string; code?: string; recovery_code?: string }): Promise<LoginResult> {
  const result = await apiRequest<LoginResult>('/api/v1/auth/login/mfa-verify', {
    method: 'POST', auth: false, body: JSON.stringify(input),
  });
  await tokenStore.setSession({
    access_token: result.access_token!, expires_in: result.expires_in!, tenant_id: result.tenant_id ?? null,
  });
  return result;
}
```
(Keep whatever the file's real existing device-id/device-name helper functions are
named — read the current `login()` implementation first and reuse them exactly, don't
invent new names.)

- [ ] **Step 3: Update `AuthContext.tsx`**

```typescript
type AuthStage = 'checking' | 'loggedOut' | 'mfaRequired' | 'loggedIn';
interface MfaChallenge { ticket: string; factor: 'totp' | 'email_otp'; }

interface AuthContextValue {
  authStage: AuthStage;
  mfaChallenge: MfaChallenge | null;
  isLoggingIn: boolean;
  loginError: string | null;
  login: (email: string, password: string) => Promise<void>;
  verifyMfa: (input: { code?: string; recovery_code?: string }) => Promise<void>;
  cancelMfa: () => void;
  logout: () => Promise<void>;
}
```

```typescript
// Replace the login() method body:
const login = useCallback(async (email: string, password: string) => {
  setIsLoggingIn(true);
  setLoginError(null);
  try {
    const result = await authApi.login(email, password);
    if (result.status === 'mfa_required') {
      setMfaChallenge({ ticket: result.mfa_ticket!, factor: result.mfa_factor! });
      setAuthStage('mfaRequired');
      return;
    }
    setAuthStage('loggedIn');
  } catch (error) {
    setLoginError(errorMessage(error));
    throw error;
  } finally {
    setIsLoggingIn(false);
  }
}, []);

const verifyMfa = useCallback(async (input: { code?: string; recovery_code?: string }) => {
  if (!mfaChallenge) throw new Error('No MFA challenge in progress.');
  setIsLoggingIn(true);
  setLoginError(null);
  try {
    await authApi.verifyMfa({ mfa_ticket: mfaChallenge.ticket, ...input });
    setMfaChallenge(null);
    setAuthStage('loggedIn');
  } catch (error) {
    setLoginError(errorMessage(error));
    throw error;
  } finally {
    setIsLoggingIn(false);
  }
}, [mfaChallenge]);

const cancelMfa = useCallback(() => {
  setMfaChallenge(null);
  setAuthStage('loggedOut');
}, []);
```

Replace the mount-time check (`tokenStore.isLoggedIn().then(setIsLoggedIn)`) with
`tokenStore.isLoggedIn().then((loggedIn) => setAuthStage(loggedIn ? 'loggedIn' : 'loggedOut'))`.
`logout()` sets `authStage` to `'loggedOut'` instead of setting a boolean.

- [ ] **Step 4: Rewrite the third `Stack.Protected` branch in `app/_layout.tsx`** — this
  is the highest-risk change in the whole plan; the file's own existing comments already
  document a prior bug where an unconditional redirect broke the app because
  `Stack.Protected` genuinely unmounts a hidden branch, not just hides it.

```typescript
function RootLayoutNav() {
  const { authStage } = useAuth();
  if (authStage === 'checking') {
    return <LoadingState label="Starting up…" />;
  }
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Protected guard={authStage === 'loggedIn'}>
        <Stack.Screen name="(tabs)" />
      </Stack.Protected>
      <Stack.Protected guard={authStage === 'mfaRequired'}>
        <Stack.Screen name="mfa" />
      </Stack.Protected>
      <Stack.Protected guard={authStage === 'loggedOut'}>
        <Stack.Screen name="login" />
      </Stack.Protected>
    </Stack>
  );
}
```

- [ ] **Step 5: Update `app/index.tsx`** to redirect into the new `mfaRequired` case too:

```typescript
export default function Index() {
  const { authStage } = useAuth();
  if (authStage === 'checking') return <LoadingState label="Starting up…" />;
  if (authStage === 'mfaRequired') return <Redirect href="/mfa" />;
  return <Redirect href={authStage === 'loggedIn' ? '/(tabs)/home' : '/login'} />;
}
```

- [ ] **Step 6: Write `app/mfa.tsx`**

```typescript
// mobile/app/app/mfa.tsx
import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { useAuth } from '../src/auth/AuthContext';
import { useColors } from '../src/theme/colors';

export default function MfaScreen() {
  const { mfaChallenge, verifyMfa, cancelMfa, isLoggingIn, loginError } = useAuth();
  const colors = useColors();
  const [code, setCode] = useState('');
  const [useRecoveryCode, setUseRecoveryCode] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState('');

  async function submit() {
    try {
      await verifyMfa(useRecoveryCode ? { recovery_code: recoveryCode } : { code });
    } catch {
      // loginError already set by AuthContext
    }
  }

  return (
    <View style={[styles.root, { backgroundColor: colors.surface }]}>
      <Text style={{ color: colors.text, fontSize: 20, fontWeight: '600' }}>Enter your code</Text>
      <Text style={{ color: colors.textMuted }}>
        {mfaChallenge?.factor === 'totp'
          ? 'Enter the 6-digit code from your authenticator app.'
          : 'Check your email for a 6-digit sign-in code.'}
      </Text>
      {loginError && <Text style={{ color: colors.critical }}>{loginError}</Text>}
      {!useRecoveryCode ? (
        <TextInput value={code} onChangeText={setCode} maxLength={6} keyboardType="number-pad" placeholder="123456" style={styles.input} />
      ) : (
        <TextInput value={recoveryCode} onChangeText={setRecoveryCode} placeholder="recovery code" style={styles.input} />
      )}
      <Pressable onPress={submit} disabled={isLoggingIn} style={[styles.button, { backgroundColor: colors.accent }]}>
        <Text style={{ color: colors.textInverse }}>{isLoggingIn ? 'Verifying…' : 'Verify'}</Text>
      </Pressable>
      {mfaChallenge?.factor === 'totp' && (
        <Pressable onPress={() => setUseRecoveryCode((v) => !v)}>
          <Text style={{ color: colors.accent }}>
            {useRecoveryCode ? 'Use my authenticator code instead' : 'Use a recovery code instead'}
          </Text>
        </Pressable>
      )}
      <Pressable onPress={cancelMfa}>
        <Text style={{ color: colors.textMuted }}>Start over</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, padding: 24, gap: 16, justifyContent: 'center' },
  input: { borderWidth: 1, borderColor: '#ccc', borderRadius: 8, padding: 12, fontSize: 18 },
  button: { padding: 14, borderRadius: 8, alignItems: 'center' },
});
```

- [ ] **Step 7: Rewrite `__tests__/AuthContext.test.tsx`** to cover the new state
  machine — at minimum: login returning `mfa_required` sets `authStage='mfaRequired'`
  and does NOT persist a session; `verifyMfa` success sets `authStage='loggedIn'` and
  does persist a session; `cancelMfa` resets to `loggedOut`; a normal (`authenticated`)
  login without any MFA step still works exactly as the current test already covers.
  Read the current test file's mocking setup (how it mocks `authApi`/`tokenStore`) and
  extend it rather than replacing its structure.

- [ ] **Step 8: Run the test suite**

Run: `cd mobile/app && npx jest AuthContext`
Expected: PASS, all cases including the new ones.

- [ ] **Step 9: Full on-device regression** (this is the step the file's own prior-bug
  comment warns is easy to get wrong): cold start while logged out → login with a TOTP
  or email-OTP account → confirm the MFA screen is the ONLY thing mounted (not layered
  over stale tabs) → verify → confirm Home renders → force-quit the app mid-MFA-entry →
  relaunch → confirm it lands back on `/login` cleanly (per the ticket's 10-minute TTL),
  not a stuck loading state.

- [ ] **Step 10: Commit**

```bash
git add mobile/app/src/auth/AuthContext.tsx mobile/app/src/api/auth.ts mobile/app/app/mfa.tsx mobile/app/app/_layout.tsx mobile/app/app/index.tsx mobile/app/__tests__/AuthContext.test.tsx
git commit -m "feat(mobile): add the three-way auth stage and login-time MFA screen"
```

---

# Part 6: Mobile — new navigation, screens, push

## Task 13: Tabs restructure — Home (3 boxes) + Settings

**Files:**
- Modify: `mobile/app/app/(tabs)/_layout.tsx`
- Create: `mobile/app/app/(tabs)/home/_layout.tsx`, `mobile/app/app/(tabs)/home/index.tsx`
- Move: `mobile/app/app/(tabs)/cameras.tsx` → `mobile/app/app/(tabs)/home/cameras/index.tsx`
- Move: `mobile/app/app/(tabs)/incidents/*` → `mobile/app/app/(tabs)/home/alerts/*`
- Create: `mobile/app/app/(tabs)/settings/_layout.tsx`, `mobile/app/app/(tabs)/settings/index.tsx`

- [ ] **Step 1: Update the tabs layout** to exactly 2 tabs:

```typescript
// app/(tabs)/_layout.tsx
export default function TabsLayout() {
  const colors = useColors();
  return (
    <Tabs screenOptions={{ headerShown: false, tabBarActiveTintColor: colors.accent }} initialRouteName="home">
      <Tabs.Screen name="home" options={{ title: 'Home', tabBarIcon: ({ color }) => <HomeIcon color={color} /> }} />
      <Tabs.Screen name="settings" options={{ title: 'Settings', tabBarIcon: ({ color }) => <AccountIcon color={color} /> }} />
    </Tabs>
  );
}
```
Add a `HomeIcon` to `src/components/Icons.tsx` if one doesn't already exist there
(check the file first — it may already export something suitable, e.g. `DashboardIcon`
renamed/reused).

- [ ] **Step 2: `home/_layout.tsx`** (a nested `Stack`, matching today's
  `incidents/_layout.tsx` shape exactly — read that file and copy its structure):

```typescript
import { Stack } from 'expo-router';
export default function HomeLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}
```

- [ ] **Step 3: `home/index.tsx`** — the 3-box screen:

```typescript
import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useColors } from '../../../src/theme/colors';
import { useBrand } from '../../../src/theme/BrandProvider';

function Box({ title, onPress }: { title: string; onPress: () => void }) {
  const colors = useColors();
  return (
    <Pressable onPress={onPress} style={[styles.box, { backgroundColor: colors.surfaceSunken, borderColor: colors.border }]}>
      <Text style={{ color: colors.text, fontSize: 16, fontWeight: '600' }}>{title}</Text>
    </Pressable>
  );
}

export default function HomeScreen() {
  const router = useRouter();
  const brand = useBrand();
  const colors = useColors();
  return (
    <View style={[styles.root, { backgroundColor: colors.surface }]}>
      <Text style={{ color: colors.text, fontSize: 22, fontWeight: '700' }}>{brand.displayName}</Text>
      <Box title="Camera" onPress={() => router.push('/(tabs)/home/cameras')} />
      <Box title="Alerts" onPress={() => router.push('/(tabs)/home/alerts')} />
      <Box title="Detections" onPress={() => router.push('/(tabs)/home/detections')} />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, padding: 16, gap: 12 },
  box: { padding: 20, borderRadius: 10, borderWidth: 1 },
});
```

- [ ] **Step 4: Move `cameras.tsx`** to `home/cameras/index.tsx` (`git mv`), add a
  `home/cameras/_layout.tsx` (same `Stack` shape as `home/_layout.tsx`), fix relative
  import depths (one level deeper), and add per-row navigation (today's list has none):
  wrap `CameraRow`'s returned `View` in a `Pressable` from `expo-router`'s `Link`
  component: `<Link href={`/(tabs)/home/cameras/${camera.id}`} asChild><Pressable>...
  </Pressable></Link>`.

- [ ] **Step 5: Move `incidents/*`** to `home/alerts/*` (`git mv` each file, fixing
  relative import depths — no other logic changes, this content is already correct).

- [ ] **Step 6: `settings/_layout.tsx`** (same `Stack` shape).

- [ ] **Step 7: `settings/index.tsx`** — Profile/Notifications/Logout, plus today's
  `account.tsx` License + Devices content folded in below Logout:

```typescript
import React from 'react';
import { ScrollView, Pressable, Text, View, StyleSheet } from 'react-native';
import { useRouter } from 'expo-router';
import { useAuth } from '../../../src/auth/AuthContext';
import { useColors } from '../../../src/theme/colors';
import { useLicense } from '../../../src/hooks/useLicense';
import { useDevices } from '../../../src/hooks/useDevices';
// ... reuse account.tsx's existing QuotaRow/device-list rendering code here, unchanged,
// just moved into this file below the new sections.

export default function SettingsScreen() {
  const router = useRouter();
  const { logout } = useAuth();
  const colors = useColors();
  return (
    <ScrollView style={{ backgroundColor: colors.surface }} contentContainerStyle={styles.content}>
      <Pressable onPress={() => router.push('/(tabs)/settings/profile')} style={styles.row}>
        <Text style={{ color: colors.text }}>Profile</Text>
      </Pressable>
      <Pressable onPress={() => router.push('/(tabs)/settings/notifications')} style={styles.row}>
        <Text style={{ color: colors.text }}>Notifications</Text>
      </Pressable>
      <Pressable onPress={logout} style={styles.row}>
        <Text style={{ color: colors.critical }}>Log out</Text>
      </Pressable>
      {/* License + Devices sections from today's account.tsx go here, content unchanged */}
    </ScrollView>
  );
}

const styles = StyleSheet.create({ content: { padding: 16, gap: 4 }, row: { paddingVertical: 14 } });
```
Copy `account.tsx`'s actual License-card and Devices-list JSX/hooks in below the three
new rows verbatim (do not re-derive it — open the current file and move its body).

- [ ] **Step 8: Delete the old `dashboard.tsx`, `cameras.tsx` (already moved),
  `incidents/*` (already moved), `account.tsx` (already moved into settings/index.tsx)**
  from their old top-level tab locations — confirm nothing still imports the old paths.

- [ ] **Step 9: Type-check and run tests**

Run: `cd mobile/app && npx tsc --noEmit && npx jest`
Expected: no type errors; existing `useIncidents.test.ts` still passes since that hook's
logic didn't change, only its screen's file location.

- [ ] **Step 10: On-device check** — cold start, confirm exactly 2 tabs, confirm Home's
  3 boxes navigate correctly, confirm Settings shows Profile/Notifications/Logout plus
  the moved License/Devices content.

- [ ] **Step 11: Commit**

```bash
git add mobile/app/app
git commit -m "feat(mobile): restructure navigation to Home (3 boxes) + Settings"
```

---

## Task 14: Camera live view (`expo-video` + HLS)

**Files:**
- Create: `mobile/app/src/hooks/useLiveSession.ts`
- Create: `mobile/app/app/(tabs)/home/cameras/[id].tsx`
- Modify: `mobile/app/src/api/tenant.ts` (add `startLiveSession`)

- [ ] **Step 1: Install the dependency**

Run: `cd mobile/app && npx expo install expo-video`

- [ ] **Step 2: Add the API function**, matching `src/api/tenant.ts`'s existing
  per-domain function convention:

```typescript
export interface LiveSession { path: string; protocol: string; token: string; play_url: string; expires_in: number; }

export async function startLiveSession(cameraId: string, protocol: 'hls' | 'webrtc' = 'hls'): Promise<LiveSession> {
  return apiRequest<LiveSession>(`/api/v1/tenant/cameras/${cameraId}/live-session`, {
    method: 'POST', body: JSON.stringify({ protocol }),
  });
}
```

- [ ] **Step 3: Write `useLiveSession`**, matching the repo's `{data, loading, error,
  refresh}` hook shape exactly (structural clone of `useDashboard.ts`):

```typescript
import { useCallback, useEffect, useState } from 'react';
import { startLiveSession, type LiveSession } from '../api/tenant';
import { errorMessage } from '../utils/errorMessage';

interface State { data: LiveSession | null; loading: boolean; error: string | null; }

export function useLiveSession(cameraId: string) {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });
  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await startLiveSession(cameraId);
      setState({ data, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
    }
  }, [cameraId]);
  useEffect(() => {
    load();
  }, [load]);
  return { ...state, refresh: load };
}
```

- [ ] **Step 4: Write the screen**

```typescript
// app/(tabs)/home/cameras/[id].tsx
import React from 'react';
import { StyleSheet, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useVideoPlayer, VideoView } from 'expo-video';
import { useLiveSession } from '../../../../src/hooks/useLiveSession';
import { LoadingState, ErrorState } from '../../../../src/components/States';

export default function CameraDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { data, loading, error, refresh } = useLiveSession(id);
  const player = useVideoPlayer(data?.play_url ?? null, (p) => { p.play(); });

  if (loading && !data) return <LoadingState label="Connecting…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  return (
    <View style={styles.root}>
      <VideoView
        player={player}
        style={styles.video}
        onError={() => refresh()}
      />
    </View>
  );
}

const styles = StyleSheet.create({ root: { flex: 1, backgroundColor: '#000' }, video: { flex: 1 } });
```
`onError` re-minting the session (rather than just showing an error) handles MediaMTX's
30-second on-demand-source close after the last viewer disconnects — a stale `play_url`
needs a fresh one, not a dead error state. Confirm `expo-video`'s real `VideoView`
error-callback prop name against its actual installed type definitions before assuming
`onError` is correct — check `node_modules/expo-video`'s types if unsure.

- [ ] **Step 5: Verify on a real device against the real Apti camera** provisioned in
  Part 4 — confirm the stream actually plays, and confirm leaving the screen open past
  30 seconds of the OTHER viewer disconnecting (if testing with two devices) still
  recovers via the refresh-on-error path.

- [ ] **Step 6: Commit**

```bash
git add mobile/app/src/hooks/useLiveSession.ts mobile/app/app/\(tabs\)/home/cameras/\[id\].tsx mobile/app/src/api/tenant.ts mobile/app/package.json mobile/app/package-lock.json
git commit -m "feat(mobile): add camera live view via expo-video HLS playback"
```

---

## Task 15: Detections screen

**Files:**
- Create: `mobile/app/src/hooks/useDetections.ts`
- Create: `mobile/app/app/(tabs)/home/detections/{_layout,index,[id]}.tsx`
- Modify: `mobile/app/src/api/types.ts`, `mobile/app/src/api/tenant.ts`

- [ ] **Step 1: Read `backend/tenant_api/app/api/detections.py`'s exact response
  models** (`DetectionOut`/`DetectionPage`/`EvidenceOut`/the bounding-box shape) and
  **read `mobile/app/src/hooks/useIncidents.ts` and `src/api/types.ts`'s
  `IncidentSummary`/cursor-pagination shape** in full before writing anything —
  `useDetections` must match `useIncidents`'s exact hook contract
  (`{items, nextCursor, loading, loadingMore, error, refresh, loadMore}`) and
  `DetectionOut`'s TypeScript mirror must match the backend's real field names exactly,
  not a guessed shape.

- [ ] **Step 2: Add the types** to `src/api/types.ts`, field-for-field matching the
  backend's `DetectionOut`/`DetectionPage`.

- [ ] **Step 3: Add `listDetections`/`getDetection`** to `src/api/tenant.ts`, matching
  `listIncidents`'s exact call-and-cursor-param convention.

- [ ] **Step 4: Write `useDetections.ts`** as a structural clone of `useIncidents.ts` —
  same cursor-pagination logic, same `stateRef` re-entrancy guard, only the underlying
  API call and item type differ.

- [ ] **Step 5: Write the list/detail screens**, matching `home/alerts/index.tsx`'s (the
  moved incidents list) rendering pattern closely (a `FlatList` + `RefreshControl` +
  `onEndReached`/`loadMore`), showing each detection's evidence image (presigned URL),
  confidence, camera/site name, captured-at time. No client-side bounding-box overlay in
  v1.

- [ ] **Step 6: Type-check and verify against the real stack**

Run: `cd mobile/app && npx tsc --noEmit`
Then on a real device: confirm the Detections list shows real rows from the Part 4
test tenant with working evidence images.

- [ ] **Step 7: Commit**

```bash
git add mobile/app/src/hooks/useDetections.ts mobile/app/app/\(tabs\)/home/detections mobile/app/src/api/types.ts mobile/app/src/api/tenant.ts
git commit -m "feat(mobile): add the Detections screen"
```

---

## Task 16: Settings → Profile

**Files:**
- Create: `mobile/app/src/hooks/useProfile.ts`
- Create: `mobile/app/app/(tabs)/settings/profile.tsx`
- Modify: `mobile/app/src/api/tenant.ts`, `mobile/app/src/api/types.ts`

- [ ] **Step 1: Add `ProfileOut` type + `getProfile()`/`getMfaStatus()` functions**,
  matching Task 4's real backend response shape and `mfa.py`'s existing
  `GET /api/v1/tenant/auth/mfa/status` shape exactly.

- [ ] **Step 2: Write `useProfile.ts`**, the same `{data, loading, error, refresh}` clone
  pattern, fetching both profile and MFA status.

- [ ] **Step 3: Write `settings/profile.tsx`** — show email/display name/organization
  name, an MFA section (enrolled: yes/no, recovery codes remaining), with an "Enroll
  TOTP" flow reusing the existing `POST /totp/enroll` + `/confirm` endpoints (a 2-step
  in-screen flow: show the `otpauth_uri`/`secret` as manual-entry text — no QR rendering
  in v1 — then a 6-digit confirm input, then display the 10 recovery codes once,
  matching the codebase's existing "shown once, never again" convention).

- [ ] **Step 4: Verify against the real stack** — enroll TOTP for a real test account
  from this screen, confirm a subsequent login actually requires that TOTP code (proving
  Task 3's `/login` change and this screen's enrollment flow are wired together
  correctly end-to-end).

- [ ] **Step 5: Commit**

```bash
git add mobile/app/src/hooks/useProfile.ts mobile/app/app/\(tabs\)/settings/profile.tsx mobile/app/src/api/tenant.ts mobile/app/src/api/types.ts
git commit -m "feat(mobile): add the Settings Profile screen with TOTP enrollment"
```

---

## Task 17: Settings → Notifications (push registration + realtime socket)

**Files:**
- Create: `mobile/app/src/hooks/useAlertsSocket.ts`
- Create: `mobile/app/app/(tabs)/settings/notifications.tsx`
- Modify: `mobile/app/src/api/tenant.ts` (push-token registration call)

- [ ] **Step 1: Install the dependency**

Run: `cd mobile/app && npx expo install expo-notifications`

- [ ] **Step 2: Add `registerPushToken(deviceId, token)`** to `src/api/tenant.ts`,
  calling Task 7's new endpoint.

- [ ] **Step 3: Register for push on login** — in `AuthContext.tsx` (or a small effect
  in the settings/notifications screen, whichever this codebase's convention favors for
  "do this once after auth" — check if `AuthContext` already has a post-login side-effect
  hook to extend, otherwise add the registration call in `settings/notifications.tsx`'s
  own mount effect):

```typescript
import * as Notifications from 'expo-notifications';

async function registerForPush(deviceId: string) {
  const { status } = await Notifications.requestPermissionsAsync();
  if (status !== 'granted') return;
  const { data: token } = await Notifications.getExpoPushTokenAsync();
  await registerPushToken(deviceId, token);
}
```

- [ ] **Step 4: Write `useAlertsSocket.ts`**, a direct port of the web CRM's
  `useIncidentSocket.ts` onto React Native's built-in `WebSocket` global (read that file
  first and mirror its ticket-mint → connect → debounce → exponential-backoff-reconnect
  logic exactly — only the transport global differs, RN's `WebSocket` vs. the browser's):

```typescript
import { useEffect, useRef } from 'react';
import { apiRequest } from '../api/client';

export function useAlertsSocket(onNudge: (incidentId: string) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  useEffect(() => {
    let cancelled = false;
    let backoffMs = 1000;

    async function connect() {
      if (cancelled) return;
      try {
        const { ticket } = await apiRequest<{ ticket: string }>('/api/v1/tenant/realtime/ws-ticket', { method: 'POST' });
        const ws = new WebSocket(`wss://<tenant-api-host>/ws/v1/tenant/incidents?ticket=${ticket}`);
        wsRef.current = ws;
        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          onNudge(data.incident_id);
        };
        ws.onclose = () => {
          if (cancelled) return;
          setTimeout(connect, backoffMs);
          backoffMs = Math.min(backoffMs * 2, 10000);
        };
      } catch {
        if (!cancelled) setTimeout(connect, backoffMs);
      }
    }
    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
    };
  }, [onNudge]);
}
```
Replace `<tenant-api-host>` with whatever this app's real base-URL config constant is
(check `src/api/config.ts`), building the `wss://` URL from it rather than hardcoding a
host.

- [ ] **Step 5: Write `notifications.tsx`** — a single toggle ("Notify me about new
  alerts while the app is open"), in-memory only (not persisted, matching the plan's own
  documented v1 simplification), wiring `useAlertsSocket` to show a local in-app banner
  when a nudge arrives while this screen (or a global provider, if the app wants
  app-wide banners — keep it screen-scoped for v1, matching the ask's literal "in that
  settings: ... notification" placement) is mounted.

- [ ] **Step 6: Verify the WS Origin question empirically** — connect this real hook to
  the real running backend from a real device build, and confirm (via temporary
  server-side logging of `websocket.headers.get("origin")` in `realtime.py`, removed
  after this check) whether React Native's WebSocket client sends any Origin header at
  all. Report the finding; if a real problem surfaces, the fix is a one-line addition to
  `settings.customer_crm_origins`-equivalent config, not a redesign.

- [ ] **Step 7: Verify push end-to-end for real** — trigger a real incident against the
  Part 4 test tenant/camera, confirm a real push notification arrives on the test device
  with the app **fully closed** (not just backgrounded) — this is the whole reason real
  push was chosen over local-only, so it must be proven working, not assumed from code
  review.

- [ ] **Step 8: Commit**

```bash
git add mobile/app/src/hooks/useAlertsSocket.ts mobile/app/app/\(tabs\)/settings/notifications.tsx mobile/app/src/api/tenant.ts mobile/app/package.json mobile/app/package-lock.json
git commit -m "feat(mobile): add push registration and the in-app alerts socket"
```

---

## Task 18: Final verification pass

**Files:** none (verification only)

- [ ] **Step 1: Full backend test suite**

Run: `cd backend && TEST_REDIS_PASSWORD=<pw> TEST_POSTGRES_DSN=<dsn> python -m pytest tests/ -v`
Expected: all tests pass, including the new `test_login_mfa_tickets.py` and
`test_expo_push_provider.py`.

- [ ] **Step 2: Full mobile check suite**

Run: `cd mobile/app && npx tsc --noEmit && npx expo-doctor && npx jest`
Expected: no type errors, doctor checks pass, all tests (existing + the rewritten
`AuthContext.test.tsx`) pass.

- [ ] **Step 3: Full end-to-end regression on a real device, using the real Apti test
  tenant/camera provisioned in Part 4**, covering every item in this plan's own earlier
  per-task manual-verification steps in one continuous pass: cold start → login (both a
  TOTP account and an email-OTP account, on separate runs) → MFA screen → Home → Camera
  live view → Alerts list (real incidents) → Detections list (real detections) →
  Settings → Profile (confirm MFA status shown correctly) → Notifications (confirm a
  real push arrives with the app closed) → Logout → confirm landing back on `/login`
  cleanly.

- [ ] **Step 4: Build the real APTI-branded APK/build**

Run: `cd mobile/app && npx eas-cli build --profile apti-preview --platform android --non-interactive`
Send the resulting install link once it completes, and confirm on install that the app
name/icon/splash are genuinely Apti's, not CSense's defaults.
