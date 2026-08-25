"""Redis-backed rotating refresh sessions (TRD §7.1), shared by every API that issues
tokens (Tenant API and Admin API both use this — the session record's `audience` field
is what keeps the two token families apart).

Only a keyed hash of the refresh token is ever stored. On every refresh call the token is
rotated: the old value is replaced, and if a client ever presents a refresh token that no
longer matches what's stored (because it was already rotated — i.e. replayed), the whole
session is revoked and the caller must re-authenticate.
"""
from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.tokens import (
    generate_refresh_token,
    hash_refresh_token,
    new_session_id,
)


def _session_key(settings: Settings, session_id: str) -> str:
    return f"cs:{settings.environment}:session:{session_id}"


async def create_session(
    redis_client: redis.Redis,
    settings: Settings,
    *,
    user_id: UUID,
    tenant_id: UUID | None,
    membership_id: UUID | None,
    audience: str,
) -> tuple[str, str]:
    """Returns (session_id, raw_refresh_token). Only the hash is persisted."""
    session_id = new_session_id()
    raw_token = generate_refresh_token()
    key = _session_key(settings, session_id)
    await redis_client.hset(
        key,
        mapping={
            "token_hash": hash_refresh_token(raw_token),
            "user_id": str(user_id),
            "tenant_id": str(tenant_id) if tenant_id else "",
            "membership_id": str(membership_id) if membership_id else "",
            "audience": audience,
        },
    )
    await redis_client.expire(key, settings.jwt_refresh_token_ttl_seconds)
    return session_id, raw_token


async def rotate_session(
    redis_client: redis.Redis, settings: Settings, *, session_id: str, presented_token: str
) -> dict[str, str] | None:
    """Returns the session record with a freshly rotated token hash, or None if the
    presented token doesn't match (expired/unknown/replayed) — callers must revoke on
    None and require re-authentication rather than silently failing open."""
    key = _session_key(settings, session_id)
    record = await redis_client.hgetall(key)
    if not record:
        return None
    if record.get("token_hash") != hash_refresh_token(presented_token):
        # Replay of an already-rotated token, or forged value: revoke the family.
        await redis_client.delete(key)
        return None

    new_token = generate_refresh_token()
    await redis_client.hset(key, "token_hash", hash_refresh_token(new_token))
    await redis_client.expire(key, settings.jwt_refresh_token_ttl_seconds)
    record["new_refresh_token"] = new_token
    return record


async def revoke_session(redis_client: redis.Redis, settings: Settings, *, session_id: str) -> None:
    await redis_client.delete(_session_key(settings, session_id))
