"""Automatic webhook delivery, driven by the outbox - the deferred half of "Webhook
signing, verification, replay protection" (CHECKLIST.md). Two passes:

`fan_out_due_outbox_events` turns each not-yet-seen `outbox_events` row into zero or more
`webhook_deliveries` rows, tracked via `processed_events` (migration 0001, unused until
this) rather than `outbox_events.published_at` - `realtime.py`'s own per-tenant WebSocket
consumer already stamps that column for an unrelated purpose, and two consumers racing to
stamp the same column would silently break whichever one lost.

`claim_and_send_one_delivery` claims one due row (`FOR UPDATE SKIP LOCKED`) and sends it,
claim and send in the *same* transaction - simpler than
`csense_shared.notifications.dispatcher.claim_due_deliveries`'s own claim-then-send split,
which exists so several worker *replicas* sharing one queue never block behind a slow
provider call. This runs as a single loop against lower expected volume, so the simpler
shape is preferred: a crash mid-POST leaves the row `pending` and untouched by any commit,
retried on the very next pass with no separate stalled-row sweep required.
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.security.envelope import EnvelopeError, KeyRing
from csense_shared.security.outbound import BlockedAddressError, resolve_public_endpoint
from csense_shared.security.secret_store import read_secret
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE, sign_payload

WEBHOOK_DISPATCH_CONSUMER = "webhook_dispatcher"

# Minutes to wait before each retry, indexed by (attempt_number - 1) at the moment the
# attempt just failed. Five retries spread over ~13 hours - long enough to ride out a
# receiver's brief outage or deploy, short enough that a permanently broken endpoint is
# abandoned well within a day rather than retried forever.
RETRY_BACKOFF_MINUTES = [1, 5, 30, 120, 720]
MAX_DELIVERY_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1  # the first attempt, plus 5 retries

# Fallback for `fan_out_due_outbox_events`'s age window when no caller passes one; the
# deployed value is `Settings.webhook_dispatch_max_event_age_seconds`, which carries the
# full reasoning. Kept as a module constant rather than read from Settings here so this
# module stays importable without configuration, the same way MAX_DELIVERY_ATTEMPTS above
# and `csense_shared.notifications.dispatcher`'s own MAX_DELAY_SECONDS already are.
DEFAULT_MAX_EVENT_AGE_SECONDS = 24 * 60 * 60

SendFn = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, int]]]


async def fan_out_due_outbox_events(
    session: AsyncSession,
    *,
    limit: int = 25,
    now: dt.datetime | None = None,
    max_event_age_seconds: float = DEFAULT_MAX_EVENT_AGE_SECONDS,
) -> int:
    """Claims up to `limit` not-yet-fanned-out outbox rows and inserts a webhook_deliveries
    row for each matching active endpoint on that row's tenant. Returns how many outbox
    rows were processed (not how many deliveries were created - one event can fan out to
    zero, one, or several endpoints). Runs inside the caller's existing transaction and
    platform-scoped session, the same convention
    `csense_shared.notifications.dispatcher.claim_due_deliveries` already uses.

    **`max_event_age_seconds` is a behavioural decision, not an optimisation.** Events
    older than the window are never selected at all, which bounds two real cases with one
    rule: on first deploy, a database's entire pre-existing outbox history falls outside
    the window, so a brand-new endpoint is not blasted with months of past events; and
    after a long worker outage only recent events go out rather than a flood of stale
    ones. This deliberately departs from the platform's usual "late alert beats no alert"
    stance (`MAX_DELAY_SECONDS`, the escalation ladder) - a webhook is an integration
    feed, and a day-old "incident created" POST is noise to a receiver, not a late alert
    to a human. Excluding by `occurred_at` rather than marking old rows in
    `processed_events` also means there is nothing to backfill and no unbounded re-scan:
    an out-of-window row can never fill a batch.
    """
    moment = now or dt.datetime.now(dt.UTC)
    cutoff = moment - dt.timedelta(seconds=max_event_age_seconds)
    rows = (
        await session.execute(
            text(
                """
                SELECT e.id, e.tenant_id, e.aggregate_type, e.aggregate_id, e.event_type,
                       e.payload, e.occurred_at
                FROM outbox_events e
                WHERE e.tenant_id IS NOT NULL
                  AND e.occurred_at > :cutoff
                  AND NOT EXISTS (
                      SELECT 1 FROM processed_events p
                      WHERE p.consumer_name = :consumer AND p.event_id = e.id
                  )
                ORDER BY e.occurred_at
                LIMIT :limit
                FOR UPDATE OF e SKIP LOCKED
                """
            ),
            {"consumer": WEBHOOK_DISPATCH_CONSUMER, "limit": limit, "cutoff": cutoff},
        )
    ).all()

    fanned = 0
    for event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload, occurred_at in rows:
        endpoints = (
            await session.execute(
                text(
                    """
                    SELECT id FROM webhook_endpoints
                    WHERE tenant_id = :tenant_id AND status = 'active'
                      AND (event_filters = '{}' OR :event_type = ANY(event_filters))
                    """
                ),
                {"tenant_id": tenant_id, "event_type": event_type},
            )
        ).all()

        delivery_payload = {
            "event": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "tenant_id": str(tenant_id),
            "occurred_at": occurred_at.isoformat(),
            "data": payload,
        }
        for (endpoint_id,) in endpoints:
            await session.execute(
                text(
                    "INSERT INTO webhook_deliveries "
                    "(tenant_id, webhook_endpoint_id, event_type, payload, status, scheduled_at, next_attempt_at) "
                    "VALUES (:tenant_id, :endpoint_id, :event_type, CAST(:payload AS jsonb), "
                    " 'pending', :now, :now)"
                ),
                {
                    "tenant_id": tenant_id, "endpoint_id": endpoint_id, "event_type": event_type,
                    "payload": json.dumps(delivery_payload), "now": moment,
                },
            )

        await session.execute(
            text(
                "INSERT INTO processed_events (consumer_name, event_id) "
                "VALUES (:consumer, :id) ON CONFLICT DO NOTHING"
            ),
            {"consumer": WEBHOOK_DISPATCH_CONSUMER, "id": event_id},
        )
        fanned += 1

    return fanned


def _split_https_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Stored webhook URL has no hostname.")
    return parsed.hostname, parsed.port or 443


async def _fail_or_abandon(
    session: AsyncSession,
    delivery_id: UUID,
    attempt_number: int,
    reason: str,
    *,
    now: dt.datetime,
    response_status: int | None = None,
    response_time_ms: int | None = None,
) -> str:
    """Records one failed attempt, and returns the status the row now holds - 'abandoned'
    once MAX_DELIVERY_ATTEMPTS is exhausted, 'failed' while a retry is still scheduled.
    Returning it (rather than letting every caller assume 'failed') keeps
    claim_and_send_one_delivery's own return value equal to the row's real state, so the
    loop above it can count "gave up permanently" separately from "will try again"."""
    if attempt_number >= MAX_DELIVERY_ATTEMPTS:
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET status = 'abandoned', next_attempt_at = NULL, "
                "attempt_number = :attempt, response_status = :rs, response_time_ms = :rt, "
                "failure_summary_redacted = :reason WHERE id = :id"
            ),
            {
                "attempt": attempt_number, "rs": response_status, "rt": response_time_ms,
                "reason": reason[:400], "id": delivery_id,
            },
        )
        return "abandoned"

    backoff_minutes = RETRY_BACKOFF_MINUTES[attempt_number - 1]
    next_attempt_at = now + dt.timedelta(minutes=backoff_minutes)
    await session.execute(
        text(
            "UPDATE webhook_deliveries SET attempt_number = :attempt, next_attempt_at = :next, "
            "response_status = :rs, response_time_ms = :rt, failure_summary_redacted = :reason "
            "WHERE id = :id"
        ),
        {
            "attempt": attempt_number + 1, "next": next_attempt_at, "rs": response_status,
            "rt": response_time_ms, "reason": reason[:400], "id": delivery_id,
        },
    )
    return "failed"


async def claim_and_send_one_delivery(
    session: AsyncSession,
    *,
    keyring: KeyRing,
    send_fn: SendFn,
    now: dt.datetime | None = None,
) -> str | None:
    """Claims and sends one due webhook_deliveries row in one transaction. Returns the
    status the row now holds - 'succeeded', 'failed' (this attempt failed and a retry is
    scheduled), 'abandoned' (permanently given up on: MAX_DELIVERY_ATTEMPTS exhausted, or
    the endpoint itself is gone/disabled) - or `None` if nothing was due. The return value
    always equals the row's own status, so a caller can count a permanent give-up
    separately from a failure that will be retried."""
    moment = now or dt.datetime.now(dt.UTC)
    row = (
        await session.execute(
            text(
                """
                SELECT id, tenant_id, webhook_endpoint_id, event_type, payload, attempt_number
                FROM webhook_deliveries
                WHERE status = 'pending' AND next_attempt_at <= :now
                ORDER BY next_attempt_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": moment},
        )
    ).first()
    if row is None:
        return None
    delivery_id, tenant_id, endpoint_id, event_type, payload, attempt_number = row

    endpoint = (
        await session.execute(
            text("SELECT url_secret_id, signing_secret_id, status::text FROM webhook_endpoints WHERE id = :id"),
            {"id": endpoint_id},
        )
    ).first()
    if endpoint is None or endpoint[2] != "active":
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET status = 'abandoned', next_attempt_at = NULL, "
                "failure_summary_redacted = 'Endpoint deleted or disabled.' WHERE id = :id"
            ),
            {"id": delivery_id},
        )
        return "abandoned"
    url_secret_id, signing_secret_id, _ = endpoint

    try:
        url = (
            await read_secret(
                session, keyring, secret_id=url_secret_id, tenant_id=tenant_id, purpose=URL_SECRET_PURPOSE,
            )
        ).decode()
        signing_secret = (
            await read_secret(
                session, keyring, secret_id=signing_secret_id, tenant_id=tenant_id,
                purpose=SIGNING_SECRET_PURPOSE,
            )
        ).decode()
    except EnvelopeError as exc:
        return await _fail_or_abandon(
            session, delivery_id, attempt_number, f"Could not decrypt endpoint secret: {exc}", now=moment
        )

    try:
        hostname, port = _split_https_url(url)
        resolve_public_endpoint(hostname, port)
    except (ValueError, BlockedAddressError) as exc:
        return await _fail_or_abandon(
            session, delivery_id, attempt_number, f"Address no longer permitted: {exc}", now=moment
        )

    body_bytes = json.dumps(payload).encode()
    signature = sign_payload(signing_secret, body_bytes)
    headers = {
        "Content-Type": "application/json",
        "X-CSense-Signature": signature,
        "X-CSense-Delivery-Id": str(delivery_id),
        "X-CSense-Event": event_type,
    }

    try:
        status_code, elapsed_ms = await send_fn(url, body_bytes, headers)
    except Exception as exc:  # noqa: BLE001 - any transport failure is retryable, not a crash
        return await _fail_or_abandon(session, delivery_id, attempt_number, str(exc), now=moment)

    if 200 <= status_code < 300:
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET status = 'succeeded', sent_at = :now, "
                "response_status = :status, response_time_ms = :ms, next_attempt_at = NULL "
                "WHERE id = :id"
            ),
            {"id": delivery_id, "now": moment, "status": status_code, "ms": elapsed_ms},
        )
        return "succeeded"

    return await _fail_or_abandon(
        session, delivery_id, attempt_number, f"Endpoint responded {status_code}.",
        now=moment, response_status=status_code, response_time_ms=elapsed_ms,
    )
