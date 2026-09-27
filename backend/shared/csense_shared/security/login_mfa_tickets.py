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
