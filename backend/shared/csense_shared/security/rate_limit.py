"""A real, Redis-backed fixed-window rate limiter for API-key-authenticated callers
(CHECKLIST: "Scoped API keys, rate limits, usage metering, developer API docs").

Fixed window, not sliding: a client can burst up to `limit` requests right at a window
boundary and again right after it (worst case, close to `2x limit` in a short span). A
sliding-window or token-bucket limiter would smooth that out, at real implementation cost
this pass doesn't need to pay - an API key's own configured `rate_limit_per_minute` is
already a per-client, operator-set ceiling, not a hard platform-wide guarantee, so the
boundary-burst imprecision is an accepted, named tradeoff rather than a silent gap.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid

import redis.asyncio as redis

from csense_shared.config import Settings


class RateLimitExceededError(Exception):
    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit of {limit}/minute exceeded - retry in {retry_after_seconds}s.")


def _window_key(settings: Settings, *, client_id: uuid.UUID, window_start: int) -> str:
    return f"cs:{settings.environment}:ratelimit:{client_id}:{window_start}"


async def check_and_increment(
    redis_client: redis.Redis, settings: Settings, *, client_id: uuid.UUID, limit_per_minute: int, at: float | None = None,
) -> None:
    """Raises `RateLimitExceededError` if this call would exceed the client's own
    per-minute limit; otherwise increments the counter and returns. The increment
    happens either way it's checked - counting the request that got refused too, so a
    client hammering the limit doesn't get a free extra request each time it's told no.
    """
    now = at if at is not None else time.time()
    window_start = int(now) - (int(now) % 60)
    key = _window_key(settings, client_id=client_id, window_start=window_start)

    count = await redis_client.incr(key)
    if count == 1:
        # Only the request that created the window sets its expiry - re-setting it on
        # every increment would let a sustained burst keep pushing the key's own expiry
        # forward forever.
        await redis_client.expire(key, 120)

    if count > limit_per_minute:
        retry_after = 60 - (int(now) % 60)
        raise RateLimitExceededError(limit=limit_per_minute, retry_after_seconds=retry_after)


def _usage_key(settings: Settings, *, client_id: uuid.UUID, date: str) -> str:
    return f"cs:{settings.environment}:api-usage:{client_id}:{date}"


async def record_usage(redis_client: redis.Redis, settings: Settings, *, client_id: uuid.UUID, at: float | None = None) -> None:
    """A separate, longer-lived counter from the rate-limit window above - rate limiting
    needs to forget after a minute; usage metering deliberately does not."""
    now = dt.datetime.fromtimestamp(at, tz=dt.UTC) if at is not None else dt.datetime.now(dt.UTC)
    date = now.strftime("%Y-%m-%d")
    key = _usage_key(settings, client_id=client_id, date=date)
    await redis_client.incr(key)
    await redis_client.expire(key, 90 * 24 * 3600)  # 90 days - enough for a real usage history view


async def get_usage(redis_client: redis.Redis, settings: Settings, *, client_id: uuid.UUID, days: int = 7) -> dict[str, int]:
    today = dt.datetime.now(dt.UTC)
    result: dict[str, int] = {}
    for offset in range(days):
        date = (today - dt.timedelta(days=offset)).strftime("%Y-%m-%d")
        raw = await redis_client.get(_usage_key(settings, client_id=client_id, date=date))
        result[date] = int(raw) if raw else 0
    return result
