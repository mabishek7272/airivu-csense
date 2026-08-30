"""Redis client + canonical tenant-prefixed keyspace helpers (SCH §13).

Keys follow `cs:{env}:{purpose}:{scope}...`. Callers should use `tenant_key()` /
`platform_key()` rather than formatting keys ad hoc, so the prefix convention can't drift.
"""
from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from csense_shared.config import Settings

# A real failure-injection test (scripts/load_test.py, CHECKLIST: "Load/spike/
# endurance/failure-injection test suite") found that with no timeout set, a Redis
# outage doesn't fail fast - it hangs the caller for however long the OS's own TCP
# connect timeout happens to be (tens of seconds, sometimes longer), which is exactly
# backwards for a dependency `GET /readyz` and the rate limiter both need to fail fast
# against. 2 seconds is generous for a private-network Redis that is actually up
# (connects and simple commands both complete in single-digit milliseconds in this
# deployment) and short enough that an outage reads as "unavailable" quickly rather than
# as a hang indistinguishable from the request never being handled at all.
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0

# Setting the socket timeouts alone was not enough - redis-py 8.x's OWN default retry
# policy (found the hard way, by timing a real failed connect: 10 retries with
# exponential backoff, ~26s total, independent of `retry_on_timeout`) silently
# multiplies whatever timeout is configured by up to 11x before finally raising. Zero
# retries here is deliberate: the caller (a request handler) already has no use for an
# automatic retry that costs many multiples of the timeout it was just given - it either
# gets a fast, clear answer or it doesn't, and callers that do want to retry (the
# rate-limit/idempotency paths) can decide that themselves at a layer that knows the
# cost of doing so.
REDIS_RETRIES = 0


def create_redis_client(settings: Settings) -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), REDIS_RETRIES),
        retry_on_timeout=False,
        retry_on_error=[],
    )


def tenant_key(settings: Settings, purpose: str, tenant_id: UUID, *parts: str) -> str:
    suffix = ":".join(parts)
    key = f"cs:{settings.environment}:{purpose}:{tenant_id}"
    return f"{key}:{suffix}" if suffix else key


def platform_key(settings: Settings, purpose: str, *parts: str) -> str:
    suffix = ":".join(parts)
    key = f"cs:{settings.environment}:{purpose}:platform"
    return f"{key}:{suffix}" if suffix else key
