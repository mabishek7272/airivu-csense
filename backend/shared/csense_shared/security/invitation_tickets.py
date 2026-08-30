"""Team invitation tokens: long-lived, single-use, Redis-backed - same shape as
`ws_tickets.py`, deliberately different lifetime and one deliberately different property.

**Different from a WS ticket in lifetime**: a WS ticket lives 20 seconds because a browser
consumes it immediately. An invitation has to survive a real person checking their email,
which can be minutes or days - `INVITATION_TTL_SECONDS` is a week.

**The same as a WS ticket in being single-use**: `GETDEL`, so a link that leaks (forwarded,
sitting in an inbox someone else can read) is redeemable exactly once - the same reasoning
`ws_tickets.py` already documents, applied here because an invitation link, once accepted,
must not let a second person claim the same membership.

The Redis key embeds the raw token as its suffix (`cs:{environment}:invitation:{token}`) -
by design, not incidentally: this is the intended way to inspect a real, uncommitted
invitation during development or e2e verification (`redis-cli KEYS
"cs:local:invitation:*"`) without the API ever having to return the token in a response
body once an email was actually sent for it - the same "don't put a secret where it
doesn't need to be" discipline `media_sessions.py`/`ws_tickets.py` already follow.
"""
from __future__ import annotations

import json
import secrets
import uuid

import redis.asyncio as redis

from csense_shared.config import Settings

INVITATION_TTL_SECONDS = 7 * 24 * 3600


def _ticket_key(settings: Settings, token: str) -> str:
    return f"cs:{settings.environment}:invitation:{token}"


async def create_invitation_ticket(
    redis_client: redis.Redis,
    settings: Settings,
    *,
    membership_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
) -> str:
    token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "membership_id": str(membership_id),
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "email": email,
        }
    )
    await redis_client.set(_ticket_key(settings, token), payload, ex=INVITATION_TTL_SECONDS)
    return token


class InvitationTicket:
    __slots__ = ("membership_id", "tenant_id", "user_id", "email")

    def __init__(self, *, membership_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID, email: str) -> None:
        self.membership_id = membership_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.email = email


async def consume_invitation_ticket(
    redis_client: redis.Redis, settings: Settings, token: str
) -> InvitationTicket | None:
    """Atomically reads and deletes the ticket - a captured or retried token cannot be
    replayed for a second acceptance. Returns None for a missing, expired, or malformed
    token, indistinguishably - either way, no invitation was verified."""
    raw = await redis_client.getdel(_ticket_key(settings, token))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return InvitationTicket(
            membership_id=uuid.UUID(data["membership_id"]),
            tenant_id=uuid.UUID(data["tenant_id"]),
            user_id=uuid.UUID(data["user_id"]),
            email=data["email"],
        )
    except (KeyError, ValueError, TypeError):
        return None
