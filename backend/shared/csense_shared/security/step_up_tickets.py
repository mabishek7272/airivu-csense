""""Recent MFA/step-up" for a high-risk action (TRD-SEC-010) - a checked-not-consumed
recency marker, the same shape `media_sessions.py` already uses for "is this still
valid", not the single-use `ws_tickets.py`/`invitation_tickets.py` shape: a step-up
verification should cover every sensitive action taken in the next few minutes, not be
burned on the first one.

Access tokens are short-lived (10-15 minutes, TRD §7.1) but carry no notion of "how
recently did this principal actually prove they still hold their second factor" - that's
what this tracks, server-side, independent of the token. Keyed by principal id (a platform
developer's `user_id` today; nothing here assumes platform-only, so a future tenant-side
step-up requirement can reuse it with a customer user id) plus a `scope` string, so a
step-up proven for one purpose doesn't quietly cover an unrelated one if scopes are ever
split later.
"""
from __future__ import annotations

import uuid

import redis.asyncio as redis

from csense_shared.config import Settings

STEP_UP_TTL_SECONDS = 5 * 60


def _key(settings: Settings, *, scope: str, principal_id: uuid.UUID) -> str:
    return f"cs:{settings.environment}:step-up:{scope}:{principal_id}"


async def mark_step_up_verified(
    redis_client: redis.Redis, settings: Settings, *, scope: str, principal_id: uuid.UUID
) -> None:
    await redis_client.set(_key(settings, scope=scope, principal_id=principal_id), "1", ex=STEP_UP_TTL_SECONDS)


async def has_recent_step_up(
    redis_client: redis.Redis, settings: Settings, *, scope: str, principal_id: uuid.UUID
) -> bool:
    return await redis_client.exists(_key(settings, scope=scope, principal_id=principal_id)) == 1
