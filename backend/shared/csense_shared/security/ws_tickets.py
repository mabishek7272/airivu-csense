"""Short-lived, single-use tickets that let a WebSocket handshake carry tenant identity.

The Tenant API authenticates every REST call with a bearer access token in the
`Authorization` header (see `csense_shared.security.tokens`) - deliberately not a cookie,
so the Customer CRM's session lives in memory only (TRD §7.1). A browser's native
WebSocket API cannot set that header on the handshake request, and the two common
workarounds both have a real cost: a query-string token sits in server access logs and
browser history for as long as those retain it, and the long-lived access token is not
something we want anywhere a log line could capture it.

So the WS handshake carries a *ticket*, not the access token itself: minted by an
authenticated REST call (`POST /realtime/ws-ticket`), opaque, good for one connection
attempt, and expired within seconds even if never used. A ticket leaking into a log is a
non-event - it is single-use and gone before most log pipelines finish writing the line.
"""
from __future__ import annotations

import json
import secrets
from uuid import UUID

import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.tenant_context import TenantContext

TICKET_TTL_SECONDS = 20


def _ticket_key(settings: Settings, ticket: str) -> str:
    return f"cs:{settings.environment}:ws-ticket:{ticket}"


async def create_ws_ticket(
    redis_client: redis.Redis, settings: Settings, *, context: TenantContext
) -> str:
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "tenant_id": str(context.tenant_id),
            "user_id": str(context.user_id),
            "membership_id": str(context.membership_id),
            "permissions": sorted(context.permissions),
        }
    )
    await redis_client.set(_ticket_key(settings, ticket), payload, ex=TICKET_TTL_SECONDS)
    return ticket


async def consume_ws_ticket(
    redis_client: redis.Redis, settings: Settings, ticket: str
) -> TenantContext | None:
    """Atomically reads and deletes the ticket, so a captured or retried ticket cannot be
    replayed for a second connection - GETDEL rather than GET-then-DELETE closes the
    window where two connections could race to use the same ticket."""
    key = _ticket_key(settings, ticket)
    raw = await redis_client.getdel(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return TenantContext(
            tenant_id=UUID(data["tenant_id"]),
            user_id=UUID(data["user_id"]),
            membership_id=UUID(data["membership_id"]),
            token_audience="csense-customer",
            permissions=frozenset(data["permissions"]),
        )
    except (KeyError, ValueError, TypeError):
        # Malformed payload is indistinguishable from "no ticket" to the caller - either
        # way, no identity was verified, so refuse rather than guess.
        return None
