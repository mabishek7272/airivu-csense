"""Real-time incident updates for the Customer CRM's inbox (Phase 5).

Two endpoints:

`POST /api/v1/tenant/realtime/ws-ticket` - an ordinary, bearer-authenticated REST call
that mints a short-lived, single-use ticket (`csense_shared.security.ws_tickets`). A
WebSocket handshake cannot carry the `Authorization` header this API otherwise requires,
so the browser fetches a ticket first and puts it on the WS URL instead of the real access
token - see that module's docstring for why.

`GET /ws/v1/tenant/incidents` - the socket itself. On connect it exchanges the ticket for
a `TenantContext` exactly once (the ticket is deleted on read, so it cannot be replayed
for a second connection), then tails `outbox_events` for that tenant and forwards new
incident events as they land.

**Why this polls the database instead of subscribing to Redis pub/sub, even though
`OutboxEvent` itself documents that shape**: pub/sub-from-a-worker means something has to
read `outbox_events` across every tenant to fan it out, and the Tenant API's own database
role deliberately cannot do that - it is not a member of the `csense_platform` group, so
it cannot see another tenant's rows even if it tried to set `app.is_platform` itself (see
README's role table), and reaching for the Admin API's `platform_session()` from here
would cross the exact boundary its own docstring rules out ("cannot be reached from
tenant API code paths"). Polling scoped to one tenant, inside that tenant's own
`tenant_session()`, needs no privileged role at all: RLS already limits it to this
tenant's rows the same way every other query in this service is limited, and the poll
naturally only runs while someone from that tenant is actually watching - nobody pays a
polling cost for a tenant with no open tab. If this ever needs to fan out across multiple
API replicas sharing one tenant's connections, that is the moment to introduce a relay;
it would be premature here, and premature in the wrong direction (a privileged read path
opened for a tenant-facing feature).

`published_at` is still stamped on rows this connection has read, matching the outbox
contract other future consumers will expect - but delivery itself never depends on
winning that race, since two tabs from the same tenant should both see every update, not
compete for one. Each connection tracks its own `(occurred_at, id)` cursor rather than
trusting `published_at` for anything but that housekeeping.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request, WebSocket
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.deps import current_tenant_context, get_app_settings
from csense_shared.config import Settings
from csense_shared.db.postgres import tenant_session
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.ws_tickets import TICKET_TTL_SECONDS, consume_ws_ticket, create_ws_ticket

logger = logging.getLogger(__name__)

router = APIRouter()

POLL_SECONDS = 1.0
BATCH_LIMIT = 50


@router.post("/api/v1/tenant/realtime/ws-ticket")
async def issue_ws_ticket(
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    require_permission(context, "incident.read")
    ticket = await create_ws_ticket(request.app.state.redis, settings, context=context)
    return {"ticket": ticket, "expires_in": TICKET_TTL_SECONDS}


async def _fetch_new_incident_events(
    session: AsyncSession, *, since_at: dt.datetime, since_id: uuid.UUID
) -> list[dict]:
    """Keyset-paginated the same way the incidents listing endpoint already is (see
    incidents.py's own cursor helpers) - a row landing between polls must not be skipped
    or resent just because two rows share a timestamp."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, event_type, aggregate_id, occurred_at
                FROM outbox_events
                WHERE aggregate_type = 'incident'
                  AND (occurred_at, id) > (:since_at, :since_id)
                ORDER BY occurred_at, id
                LIMIT :limit
                """
            ),
            {"since_at": since_at, "since_id": since_id, "limit": BATCH_LIMIT},
        )
    ).all()
    return [
        {"id": r[0], "event_type": r[1], "incident_id": r[2], "occurred_at": r[3]}
        for r in rows
    ]


async def _mark_published(session: AsyncSession, event_ids: list[uuid.UUID]) -> None:
    if not event_ids:
        return
    await session.execute(
        text(
            "UPDATE outbox_events SET published_at = now() "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND published_at IS NULL"
        ),
        {"ids": event_ids},
    )


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    """This connection is otherwise server-push only, but something still has to notice
    when the client goes away - without reading its close frame, the loop below would
    keep polling and calling send_text on a dead socket until the framework eventually
    notices on its own."""
    while True:
        await websocket.receive_text()


_NIL_UUID = uuid.UUID(int=0)


async def _poll_and_forward(
    websocket: WebSocket,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    # Starts from connection time, not from the beginning of history - a tab that was
    # closed and reopened gets a REST refetch for whatever it missed (the frontend
    # already does one on connect), not a backlog replayed over the socket.
    cursor_at = dt.datetime.now(dt.UTC)
    cursor_id = _NIL_UUID
    while True:
        async with tenant_session(session_factory, tenant_id) as session:
            events = await _fetch_new_incident_events(session, since_at=cursor_at, since_id=cursor_id)
            if events:
                await _mark_published(session, [e["id"] for e in events])
        for event in events:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": event["event_type"],
                        "incident_id": event["incident_id"],
                        "occurred_at": event["occurred_at"].isoformat(),
                    }
                )
            )
            cursor_at, cursor_id = event["occurred_at"], event["id"]
        await asyncio.sleep(POLL_SECONDS)


@router.websocket("/ws/v1/tenant/incidents")
async def incidents_socket(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings

    # WS handshakes aren't covered by the CORS middleware (browsers don't preflight
    # them), so this is the one place that boundary has to be checked by hand - same
    # allowlist the REST CORS policy already uses, not a new one to keep in sync.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.customer_crm_origins:
        await websocket.close(code=1008)
        return

    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=1008)
        return

    context = await consume_ws_ticket(websocket.app.state.redis, settings, ticket)
    if context is None or not context.has_permission("incident.read"):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    forward_task = asyncio.create_task(
        _poll_and_forward(websocket, websocket.app.state.session_factory, context.tenant_id)
    )
    disconnect_task = asyncio.create_task(_watch_for_disconnect(websocket))
    try:
        done, pending = await asyncio.wait(
            {forward_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception("incident_socket_task_failed", exc_info=exc)
    finally:
        forward_task.cancel()
        disconnect_task.cancel()
        for task in (forward_task, disconnect_task):
            try:
                await task
            except BaseException:  # noqa: BLE001 - cleanup path: cancellation and a
                # dead-socket send both land here, neither is worth reporting.
                pass
