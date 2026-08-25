"""Redis client + canonical tenant-prefixed keyspace helpers (SCH §13).

Keys follow `cs:{env}:{purpose}:{scope}...`. Callers should use `tenant_key()` /
`platform_key()` rather than formatting keys ad hoc, so the prefix convention can't drift.
"""
from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from csense_shared.config import Settings


def create_redis_client(settings: Settings) -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        decode_responses=True,
    )


def tenant_key(settings: Settings, purpose: str, tenant_id: UUID, *parts: str) -> str:
    suffix = ":".join(parts)
    key = f"cs:{settings.environment}:{purpose}:{tenant_id}"
    return f"{key}:{suffix}" if suffix else key


def platform_key(settings: Settings, purpose: str, *parts: str) -> str:
    suffix = ":".join(parts)
    key = f"cs:{settings.environment}:{purpose}:platform"
    return f"{key}:{suffix}" if suffix else key
