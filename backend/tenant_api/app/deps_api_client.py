"""Authenticating an external integration calling through an API key, as opposed to a
person with a session or a device with an enrolment credential (`deps_agent.py`).

Same shape as `deps_agent.py`, deliberately: prefix + SHA-256-digest lookup through a
`SECURITY DEFINER` function, because resolving the credential is what discovers the
tenant, and "every failure looks the same" so a caller with a list of guessed prefixes
learns nothing from which error it gets back.

**A separate context type, not a widened `TenantContext`.** `require_permission()` is
typed to `TenantContext | PlatformContext` specifically so a function written for one
cannot accidentally run with the other's assumptions. An API-key caller is neither - it
has no membership, no user session, and its permissions are a *subset* of some person's
permissions at the moment the key was issued, frozen at issuance rather than reflecting
that person's access today. `ApiClientContext` gets its own narrow `require_scope()`
rather than being folded into `require_permission()`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import logging
import uuid

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.errors import ApiError
from csense_shared.security.api_keys import KEY_PREFIX_LENGTH
from csense_shared.security.rate_limit import RateLimitExceededError, check_and_increment, record_usage

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ApiClientContext:
    """Who is calling, when the caller is an external integration using an API key."""

    api_client_id: str
    key_id: str
    tenant_id: str
    name: str
    scopes: frozenset[str]
    site_scope_mode: str
    rate_limit_per_minute: int

    def has_scope(self, code: str) -> bool:
        return code in self.scopes


def _rejection() -> ApiError:
    return ApiError(
        status_code=401,
        code="api_client_unauthenticated",
        message="That API key is not valid.",
    )


def _presented_key(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise _rejection()
    return value.strip()


async def current_api_client(request: Request) -> ApiClientContext:
    """Resolves the presented API key, or refuses.

    Opens its own session rather than depending on the tenant-scoped one - like
    `current_agent`, the tenant is not yet known when this runs.
    """
    presented = _presented_key(request)
    prefix = presented[:KEY_PREFIX_LENGTH]
    digest = hashlib.sha256(presented.encode()).hexdigest()

    factory = request.app.state.session_factory
    async with factory() as db:
        row = (
            await db.execute(text("SELECT * FROM api_key_lookup(:prefix)"), {"prefix": prefix})
        ).first()

        if row is None:
            logger.warning("api_client_auth_unknown_prefix", extra={"key_prefix": prefix})
            raise _rejection()

        (
            key_id, api_client_id, tenant_id, secret_hash, key_revoked_at, key_expires_at,
            client_name, client_status, client_scopes, client_site_scope_mode,
            client_rate_limit_per_minute, client_expires_at,
        ) = row

        # Constant-time, and on the digest rather than the presented key.
        if not hmac.compare_digest(secret_hash or "", digest):
            logger.warning("api_client_auth_bad_credential", extra={"key_prefix": prefix})
            raise _rejection()

        if key_revoked_at is not None or client_status != "active":
            logger.warning("api_client_auth_inactive", extra={"api_client_id": str(api_client_id)})
            raise _rejection()

        now = dt.datetime.now(dt.UTC)
        for expiry in (key_expires_at, client_expires_at):
            if expiry is not None and expiry <= now:
                logger.warning("api_client_auth_expired", extra={"api_client_id": str(api_client_id)})
                raise _rejection()

        # Best-effort - a caller learning "when was this key last used" matters for
        # operators auditing stale keys, but failing the request over it would turn an
        # observability nicety into an outage.
        try:
            await db.execute(
                text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"), {"id": key_id}
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("api_client_last_used_update_failed", extra={"key_id": str(key_id)})

    return ApiClientContext(
        api_client_id=str(api_client_id),
        key_id=str(key_id),
        tenant_id=str(tenant_id),
        name=client_name,
        scopes=frozenset(client_scopes or []),
        site_scope_mode=client_site_scope_mode,
        rate_limit_per_minute=client_rate_limit_per_minute,
    )


def require_scope(client: ApiClientContext, code: str) -> None:
    if not client.has_scope(code):
        raise ApiError(
            status_code=403, code="scope_not_granted",
            message=f"This API key was not issued the '{code}' scope.",
        )


async def api_client_db_session(
    request: Request, client: ApiClientContext = Depends(current_api_client)
) -> AsyncSession:
    """A session scoped to the calling client's tenant - the scope comes from the
    credential, exactly as `agent_db_session` derives it from a device's credential."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": client.tenant_id}
        )
        yield session


async def enforce_rate_limit(
    request: Request, client: ApiClientContext = Depends(current_api_client)
) -> ApiClientContext:
    """A dependency, not something buried inside a handler - every API-key-authenticated
    route pays for the check the same way, and a route that forgets to depend on this one
    is visibly missing it in its own signature.

    Usage is recorded before the limit is even checked, deliberately - a refused call
    still cost the platform a request to authenticate and evaluate, and a caller hammering
    past its own limit should see that reflected in its usage history, not undercounted
    relative to what it actually sent."""
    redis_client = request.app.state.redis
    settings = request.app.state.settings
    client_uuid = uuid.UUID(client.api_client_id)

    await record_usage(redis_client, settings, client_id=client_uuid)

    try:
        await check_and_increment(
            redis_client, settings, client_id=client_uuid, limit_per_minute=client.rate_limit_per_minute,
        )
    except RateLimitExceededError as exc:
        raise ApiError(
            status_code=429, code="rate_limit_exceeded", message=str(exc), retryable=True,
            details={"limit_per_minute": exc.limit, "retry_after_seconds": exc.retry_after_seconds},
        ) from exc

    return client
