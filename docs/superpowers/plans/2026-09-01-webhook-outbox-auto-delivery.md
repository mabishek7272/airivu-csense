# Webhook Outbox Auto-Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive real, automatic webhook delivery off the outbox for every domain event —
closing the "automatic delivery" half CHECKLIST.md's webhook entry deliberately deferred
(signing, verification, SSRF-checked on-demand test delivery, and the full delivery-history
schema already shipped; nothing today fires a webhook on its own when an incident, camera,
or license event actually happens).

**Architecture:** Two new passes, run as a second concurrent loop inside the *existing*
`notification-worker` container (not a new service) — reusing its already-deployed platform
DB role, the same "dispatch legitimately spans every tenant" justification that container's
own docstring already gives, and this project's general "reuse infra, don't proliferate
containers" discipline on a 16-core box (`CLAUDE.md`).

**Fan-out**: turns each not-yet-seen `outbox_events` row into zero or more
`webhook_deliveries` rows (one per matching active endpoint for that tenant), tracked via
`processed_events` — a dedicated multi-consumer idempotency table that's existed since
migration 0001 but nothing has used yet. This deliberately does **not** reuse
`outbox_events.published_at`: `realtime.py`'s per-tenant WebSocket consumer already stamps
that column for an unrelated purpose (forwarding incident events to an open browser tab),
and two consumers racing to stamp the same column would silently break whichever one loses.

**Send**: claims one due, retryable `webhook_deliveries` row at a time (`FOR UPDATE SKIP
LOCKED`) and sends it — decrypt URL + signing secret (`secret_store.read_secret`, the same
envelope-encrypted path the on-demand test delivery already uses), a fresh SSRF check
(`resolve_public_endpoint` — a public DNS record can be repointed after the endpoint was
created), sign (`csense_shared.security.webhooks.sign_payload`), POST, record the result.
Deliberately simpler than `notification_worker`'s own claim-then-send split (which exists
so a slow provider call never blocks a lock several *replica* workers are sharing) — this
runs as one loop, and a crash mid-POST just leaves the row `pending`, retried on the very
next pass with no separate stalled-row recovery step needed.

**Tech Stack:** Same as the existing `notification_worker` service — asyncio polling loop,
SQLAlchemy async raw SQL (`FOR UPDATE SKIP LOCKED`, mirroring
`csense_shared.notifications.dispatcher.claim_due_deliveries` exactly), httpx for outbound
delivery, pytest + pytest-asyncio against a real migrated test database, a real-stack e2e
script against `httpbin.org` (the same "prove it against something real" precedent
`scripts/e2e_webhooks.py` already set).

---

### Task 1: Share the webhook secret-purpose constants — ✅ DONE (commit `90e6bb3`)

`URL_SECRET_PURPOSE` / `SIGNING_SECRET_PURPOSE` now live in
`csense_shared.security.webhooks`; `backend/tenant_api/app/api/webhooks.py` imports them
from there instead of defining its own copies. The notification-worker's dispatcher (a
different deployable service, which only ever imports from `csense_shared`) needs the
identical strings to decrypt the same secrets.

---

### Task 2: `csense_shared/webhooks/dispatcher.py` — fan-out and send, real-DB tested — ✅ DONE (commit `8e57df9`)

> **Shipped with four deliberate corrections to the draft below — the code is the truth,
> this section is kept for its reasoning.** (1) The fixture stores a public IP *literal*,
> not `https://example.test/hook`: `.test` never resolves, so the real SSRF re-check
> rejected every delivery before the injected `send_fn` ran — which had also made the
> retry test pass for the wrong reason. (2) The fixture marks the pre-existing outbox
> backlog as already seen by this consumer, or that backlog (selected oldest-first) fills
> the batch ahead of the test's own event. (3) `_fail_or_abandon` now returns the status
> the row actually holds and callers propagate it, resolving a contradiction between the
> draft's implementation (returned `"failed"` after abandoning) and its own test. (4) A
> `max_event_age_seconds` window (24h, `Settings.webhook_dispatch_max_event_age_seconds`)
> bounds fan-out, so a first deploy never blasts a database's whole outbox history at a
> new endpoint — 11 tests, not 9.

The testable core, mirroring `csense_shared.notifications.dispatcher`'s own placement and
style exactly (a top-level `webhooks` package alongside `notifications`, both siblings of
`security`).

**Files:**
- Create: `backend/shared/csense_shared/webhooks/__init__.py`
- Create: `backend/shared/csense_shared/webhooks/dispatcher.py`
- Test: `backend/tests/test_webhook_dispatcher.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_webhook_dispatcher.py`:

```python
"""fan_out_due_outbox_events() and claim_and_send_one_delivery() - the two passes behind
automatic webhook delivery. Real DB (migrations 0001/0045 already provide every table
used here - no new migration). `send_fn` is injected so no real network call happens in
these tests; scripts/e2e_webhook_dispatch.py proves the real HTTP path against
httpbin.org, the same split test_webhooks.py/e2e_webhooks.py already uses.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.security.envelope import KeyRing, generate_master_key
from csense_shared.security.secret_store import write_secret
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE
from csense_shared.webhooks.dispatcher import (
    MAX_DELIVERY_ATTEMPTS,
    WEBHOOK_DISPATCH_CONSUMER,
    claim_and_send_one_delivery,
    fan_out_due_outbox_events,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _async_dsn(env_var: str = "TEST_POSTGRES_DSN") -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing({"v1": generate_master_key()}, "v1")


@pytest_asyncio.fixture()
async def ctx(keyring):
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Webhook Dispatch Test {suffix}", "s": f"webhook-dispatch-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()

        url_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=URL_SECRET_PURPOSE,
            plaintext="https://example.test/hook", label="test",
        )
        signing_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=SIGNING_SECRET_PURPOSE,
            plaintext="whsec_test_secret", label="test",
        )
        endpoint_id = (
            await session.execute(
                text(
                    "INSERT INTO webhook_endpoints "
                    "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, event_filters, status) "
                    "VALUES (:t, 'Test Endpoint', :url_id, 'example.test', :sig_id, :filters, 'active') RETURNING id"
                ),
                {"t": tenant_id, "url_id": url_secret_id, "sig_id": signing_secret_id, "filters": []},
            )
        ).scalar_one()

    yield {
        "factory": factory, "tenant_id": tenant_id, "org_id": org_id, "endpoint_id": endpoint_id,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM webhook_endpoints WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(
            text("DELETE FROM processed_events WHERE consumer_name = :c"), {"c": WEBHOOK_DISPATCH_CONSUMER}
        )
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})

    await engine.dispose()


async def _insert_outbox_event(ctx, *, event_type: str = "incident.created.v1") -> uuid.UUID:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        event_id = (
            await session.execute(
                text(
                    "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                    "VALUES (:t, 'incident', :agg, :et, CAST(:p AS jsonb)) RETURNING id"
                ),
                {"t": ctx["tenant_id"], "agg": str(uuid.uuid4()), "et": event_type, "p": json.dumps({"x": 1})},
            )
        ).scalar_one()
    return event_id


async def _fake_send(status_code: int, elapsed_ms: int = 42):
    async def send_fn(url, body, headers):
        return status_code, elapsed_ms
    return send_fn


@pytest.mark.asyncio
async def test_fan_out_creates_one_delivery_per_matching_active_endpoint(ctx):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        fanned = await fan_out_due_outbox_events(session, limit=25)
        rows = (
            await session.execute(
                text("SELECT status, event_type FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).all()
    assert fanned == 1
    assert len(rows) == 1
    assert rows[0][0] == "pending"
    assert rows[0][1] == "incident.created.v1"


@pytest.mark.asyncio
async def test_fan_out_is_idempotent_across_passes(ctx):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        second_pass = await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert second_pass == 0
    assert count == 1


@pytest.mark.asyncio
async def test_fan_out_skips_endpoints_that_do_not_match_event_filters(ctx):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(
            text("UPDATE webhook_endpoints SET event_filters = :f WHERE id = :id"),
            {"f": ["camera.offline.v1"], "id": ctx["endpoint_id"]},
        )
    await _insert_outbox_event(ctx, event_type="incident.created.v1")
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_claim_and_send_marks_success_on_2xx(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)

    send_fn = await _fake_send(200)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text("SELECT status, response_status, next_attempt_at FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "succeeded"
    assert row[0] == "succeeded"
    assert row[1] == 200
    assert row[2] is None


@pytest.mark.asyncio
async def test_claim_and_send_schedules_a_retry_on_failure_status(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)

    send_fn = await _fake_send(500)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text(
                    "SELECT status, attempt_number, next_attempt_at > now() "
                    "FROM webhook_deliveries WHERE tenant_id = :t"
                ),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "failed"
    assert row[0] == "pending"  # still retryable
    assert row[1] == 2
    assert row[2] is True


@pytest.mark.asyncio
async def test_claim_and_send_abandons_after_max_attempts(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET attempt_number = :n, next_attempt_at = now() WHERE tenant_id = :t"
            ),
            {"n": MAX_DELIVERY_ATTEMPTS, "t": ctx["tenant_id"]},
        )

    send_fn = await _fake_send(500)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
        row = (
            await session.execute(
                text("SELECT status, next_attempt_at FROM webhook_deliveries WHERE tenant_id = :t"),
                {"t": ctx["tenant_id"]},
            )
        ).first()
    assert status == "abandoned"
    assert row[0] == "abandoned"
    assert row[1] is None


@pytest.mark.asyncio
async def test_claim_and_send_returns_none_when_nothing_is_due(ctx, keyring):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=await _fake_send(200))
    assert status is None


@pytest.mark.asyncio
async def test_disabled_endpoint_abandons_the_delivery_without_sending(ctx, keyring):
    await _insert_outbox_event(ctx)
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await fan_out_due_outbox_events(session, limit=25)
        await session.execute(
            text("UPDATE webhook_endpoints SET status = 'disabled' WHERE id = :id"), {"id": ctx["endpoint_id"]}
        )

    called = False

    async def send_fn(url, body, headers):
        nonlocal called
        called = True
        return 200, 1

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        status = await claim_and_send_one_delivery(session, keyring=keyring, send_fn=send_fn)
    assert status == "abandoned"
    assert called is False


@pytest.mark.asyncio
async def test_a_platform_level_event_with_no_tenant_is_never_fanned_out(ctx):
    # outbox_events.tenant_id is nullable - a platform-level event belongs to no tenant
    # and so has no tenant's webhook endpoints to deliver to.
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(
            text(
                "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                "VALUES (NULL, 'platform', :agg, 'platform.something.v1', '{}'::jsonb)"
            ),
            {"agg": str(uuid.uuid4())},
        )
        fanned = await fan_out_due_outbox_events(session, limit=25)
        count = (
            await session.execute(
                text("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).scalar_one()
    assert fanned == 0
    assert count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && TEST_POSTGRES_DSN="$TEST_POSTGRES_DSN" python -m pytest tests/test_webhook_dispatcher.py -v`
Expected: `ModuleNotFoundError: No module named 'csense_shared.webhooks'`.

- [ ] **Step 3: Write the implementation**

Create `backend/shared/csense_shared/webhooks/__init__.py` (empty file — marks the package,
matching `csense_shared/notifications/__init__.py`'s own convention).

Create `backend/shared/csense_shared/webhooks/dispatcher.py`:

```python
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

SendFn = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, int]]]


async def fan_out_due_outbox_events(
    session: AsyncSession, *, limit: int = 25, now: dt.datetime | None = None
) -> int:
    """Claims up to `limit` not-yet-fanned-out outbox rows and inserts a webhook_deliveries
    row for each matching active endpoint on that row's tenant. Returns how many outbox
    rows were processed (not how many deliveries were created - one event can fan out to
    zero, one, or several endpoints). Runs inside the caller's existing transaction and
    platform-scoped session, the same convention
    `csense_shared.notifications.dispatcher.claim_due_deliveries` already uses."""
    moment = now or dt.datetime.now(dt.UTC)
    rows = (
        await session.execute(
            text(
                """
                SELECT e.id, e.tenant_id, e.aggregate_type, e.aggregate_id, e.event_type,
                       e.payload, e.occurred_at
                FROM outbox_events e
                WHERE e.tenant_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM processed_events p
                      WHERE p.consumer_name = :consumer AND p.event_id = e.id
                  )
                ORDER BY e.occurred_at
                LIMIT :limit
                FOR UPDATE OF e SKIP LOCKED
                """
            ),
            {"consumer": WEBHOOK_DISPATCH_CONSUMER, "limit": limit},
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
) -> None:
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
        return

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


async def claim_and_send_one_delivery(
    session: AsyncSession,
    *,
    keyring: KeyRing,
    send_fn: SendFn,
    now: dt.datetime | None = None,
) -> str | None:
    """Claims and sends one due webhook_deliveries row in one transaction. Returns
    'succeeded', 'failed' (a retry was scheduled, or the delivery was abandoned after
    MAX_DELIVERY_ATTEMPTS), 'abandoned' (the endpoint itself is gone/disabled), or `None`
    if nothing was due."""
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
        await _fail_or_abandon(
            session, delivery_id, attempt_number, f"Could not decrypt endpoint secret: {exc}", now=moment
        )
        return "failed"

    try:
        hostname, port = _split_https_url(url)
        resolve_public_endpoint(hostname, port)
    except (ValueError, BlockedAddressError) as exc:
        await _fail_or_abandon(
            session, delivery_id, attempt_number, f"Address no longer permitted: {exc}", now=moment
        )
        return "failed"

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
        await _fail_or_abandon(session, delivery_id, attempt_number, str(exc), now=moment)
        return "failed"

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

    await _fail_or_abandon(
        session, delivery_id, attempt_number, f"Endpoint responded {status_code}.",
        now=moment, response_status=status_code, response_time_ms=elapsed_ms,
    )
    return "failed"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && TEST_POSTGRES_DSN="$TEST_POSTGRES_DSN" python -m pytest tests/test_webhook_dispatcher.py -v`
Expected: 9 passed.

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && python -m pytest -q && ruff check shared tests`
Expected: all previously-passing tests still pass; 9 new tests pass; ruff clean. (One
pre-existing, unrelated failure is known and expected:
`test_site_timezones.py::test_unusable_values_are_refused[asia/kolkata]`.)

- [ ] **Step 6: Commit**

```bash
git add backend/shared/csense_shared/webhooks/ backend/tests/test_webhook_dispatcher.py
git commit -m "Add fan_out_due_outbox_events() and claim_and_send_one_delivery()

The testable core of automatic webhook delivery. Fan-out tracks progress via
processed_events (migration 0001, unused until now) rather than
outbox_events.published_at, which realtime.py's own per-tenant consumer already owns.
9 real-DB tests: fan-out creates one delivery per matching endpoint, is idempotent
across passes, respects event_filters, a platform-level (tenant-less) event is never
fanned out, success/retry-backoff/abandon-after-max-attempts, nothing-due returns None,
a disabled endpoint abandons without ever sending.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire it into the `notification-worker` container as a second loop

**Files:**
- Modify: `backend/shared/csense_shared/config.py`
- Create: `backend/notification_worker/app/webhook_dispatch.py`
- Modify: `backend/notification_worker/app/main.py`
- Test: `backend/tests/test_webhook_dispatch_loop.py`

- [ ] **Step 1: Add poll-interval/batch-size settings**

Edit `backend/shared/csense_shared/config.py` — immediately after the existing
`notification_batch_size: int = 25` line, add:

```python
    # Webhook auto-delivery, run as a second loop inside the same notification-worker
    # container (see notification_worker/app/webhook_dispatch.py). A longer interval than
    # notification_poll_seconds is fine here: a webhook is a developer-facing integration,
    # not a life-safety alert, and outbound HTTP to an arbitrary third party should not be
    # attempted as tightly as an internal DB claim query.
    webhook_dispatch_poll_seconds: float = 10.0
    webhook_dispatch_batch_size: int = 25
```

- [ ] **Step 2: Write the failing test for the loop wrapper**

Create `backend/tests/test_webhook_dispatch_loop.py` — loaded by file path, the same
convention `test_notification_worker.py` already established (every service names its
package `app`, so a plain import would resolve ambiguously):

```python
"""notification_worker/app/webhook_dispatch.py - the thin run_once/run_forever loop on
top of csense_shared.webhooks.dispatcher (already covered directly by
test_webhook_dispatcher.py). Loaded by file path, the same convention
test_notification_worker.py already established.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.security.envelope import KeyRing, generate_master_key
from csense_shared.security.secret_store import write_secret
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE
from csense_shared.webhooks.dispatcher import WEBHOOK_DISPATCH_CONSUMER

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

# NOTE (learned the hard way in Task 2 — do not "simplify" either of these):
#
#   The endpoint URL below is a public IP *literal*, not a hostname like
#   `https://example.test/hook`. `claim_and_send_one_delivery` re-runs the real SSRF guard
#   (`resolve_public_endpoint`) immediately before sending, and `.test` is an RFC 2606
#   reserved TLD that by definition never resolves — `getaddrinfo` raises, the guard turns
#   that into `BlockedAddressError`, and the delivery fails before the injected `send_fn`
#   is ever called. A numeric host is resolved locally, so the real guard still runs and
#   still makes no network call. (RFC 5737 documentation ranges like 192.0.2.0/24 do NOT
#   work either — Python's `ipaddress` classifies them private, so the guard blocks them.)
#
#   The fixture also marks every pre-existing outbox row as already seen by this consumer.
#   `processed_events` starts empty in any real database, so without this the whole
#   existing outbox backlog (which fan-out selects oldest-first) fills the batch ahead of
#   the row the test just inserted, and `assert stats["fanned"] == 1` gets a much larger
#   number. The 24h age window added in Task 2 does NOT make this unnecessary — recent
#   backlog sits inside the window too, which only makes the failure intermittent instead
#   of fixing it.


def _load_webhook_dispatch():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "notification_worker" / "app" / "webhook_dispatch.py"
    )
    spec = importlib.util.spec_from_file_location("webhook_dispatch_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _async_dsn(env_var: str = "TEST_POSTGRES_DSN") -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing({"v1": generate_master_key()}, "v1")


@pytest_asyncio.fixture()
async def ctx(keyring):
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Webhook Loop Test {suffix}", "s": f"webhook-loop-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        url_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=URL_SECRET_PURPOSE,
            plaintext="https://93.184.216.34/hook", label="test",
        )
        signing_secret_id = await write_secret(
            session, keyring, tenant_id=tenant_id, purpose=SIGNING_SECRET_PURPOSE,
            plaintext="whsec_test_secret", label="test",
        )
        await session.execute(
            text(
                "INSERT INTO webhook_endpoints "
                "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, event_filters, status) "
                "VALUES (:t, 'Test', :url_id, '93.184.216.34', :sig_id, :filters, 'active')"
            ),
            {"t": tenant_id, "url_id": url_secret_id, "sig_id": signing_secret_id, "filters": []},
        )
        # Everything already in the outbox is this consumer's backlog, not this test's
        # event - mark it seen up front (exactly what the real dispatcher's first pass
        # does) so the assertions below measure only the row inserted next.
        await session.execute(
            text(
                "INSERT INTO processed_events (consumer_name, event_id) "
                "SELECT :c, id FROM outbox_events ON CONFLICT DO NOTHING"
            ),
            {"c": WEBHOOK_DISPATCH_CONSUMER},
        )
        await session.execute(
            text(
                "INSERT INTO outbox_events (tenant_id, aggregate_type, aggregate_id, event_type, payload) "
                "VALUES (:t, 'incident', :agg, 'incident.created.v1', '{}'::jsonb)"
            ),
            {"t": tenant_id, "agg": str(uuid.uuid4())},
        )

    yield {"factory": factory, "tenant_id": tenant_id}

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        await session.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM webhook_endpoints WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
        await session.execute(
            text("DELETE FROM processed_events WHERE consumer_name = :c"), {"c": WEBHOOK_DISPATCH_CONSUMER}
        )
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_once_fans_out_and_sends(ctx, keyring, monkeypatch):
    # run_once uses the real httpx-based sender by default; this test replaces it so no
    # real network call happens, while still proving fan-out -> send connects end to end
    # through the loop wrapper (not just through the dispatcher module directly, which
    # test_webhook_dispatcher.py already covers in isolation).
    module = _load_webhook_dispatch()

    async def fake_http_post(url, body, headers):
        return 200, 7

    monkeypatch.setattr(module, "_http_post", fake_http_post)

    stats = await module.run_once(ctx["factory"], keyring, batch_size=10)
    assert stats["fanned"] == 1
    assert stats["sent"] == 1

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
        row = (
            await session.execute(
                text("SELECT status FROM webhook_deliveries WHERE tenant_id = :t"), {"t": ctx["tenant_id"]}
            )
        ).first()
    assert row[0] == "succeeded"


@pytest.mark.asyncio
async def test_run_once_is_a_no_op_pass_when_nothing_is_due(ctx, keyring, monkeypatch):
    module = _load_webhook_dispatch()

    async def fake_http_post(url, body, headers):
        return 200, 1

    monkeypatch.setattr(module, "_http_post", fake_http_post)

    await module.run_once(ctx["factory"], keyring, batch_size=10)  # drains the one seeded event
    stats = await module.run_once(ctx["factory"], keyring, batch_size=10)
    assert stats == {"fanned": 0, "sent": 0}
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd backend && TEST_POSTGRES_DSN="$TEST_POSTGRES_DSN" python -m pytest tests/test_webhook_dispatch_loop.py -v`
Expected: `FileNotFoundError` (or similar) — `notification_worker/app/webhook_dispatch.py`
doesn't exist yet.

- [ ] **Step 4: Write the loop wrapper**

Create `backend/notification_worker/app/webhook_dispatch.py`:

```python
"""Drives automatic webhook delivery from the outbox - the deferred half of "Webhook
signing, verification, replay protection" (CHECKLIST.md). Runs as a second concurrent
loop inside this same notification-worker container/process (see main.py), not a new
container - reusing the exact platform DB role and docker-compose service that already
exists for the "dispatch legitimately spans every tenant" reason this container's own
worker.py already states for notifications.

The actual fan-out/send logic lives in csense_shared.webhooks.dispatcher (directly
tested there, against a real database, without any of this loop machinery) - this module
is only the polling wrapper, the same split worker.py itself has with
csense_shared.notifications.dispatcher.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time

import httpx
from sqlalchemy import text

from csense_shared.security.envelope import KeyRing
from csense_shared.webhooks.dispatcher import claim_and_send_one_delivery, fan_out_due_outbox_events

logger = logging.getLogger(__name__)

SEND_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def _http_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, int]:
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=SEND_REQUEST_TIMEOUT) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code, int((time.monotonic() - started) * 1000)


async def _as_platform(session) -> None:
    await session.execute(text("SELECT set_config('app.is_platform','true',true)"))


async def run_once(
    session_factory, keyring: KeyRing, *, batch_size: int = 25, now: dt.datetime | None = None
) -> dict:
    """One pass: fan out due outbox events, then send up to `batch_size` due deliveries."""
    moment = now or dt.datetime.now(dt.UTC)

    async with session_factory() as session, session.begin():
        await _as_platform(session)
        fanned = await fan_out_due_outbox_events(session, limit=batch_size, now=moment)

    sent = 0
    for _ in range(batch_size):
        async with session_factory() as session, session.begin():
            await _as_platform(session)
            try:
                status = await claim_and_send_one_delivery(
                    session, keyring=keyring, send_fn=_http_post, now=moment
                )
            except Exception:
                # One poisonous delivery must not stop the pass - the row stays `pending`
                # and is picked up again next pass, the same "crash loses nothing" property
                # claim_and_send_one_delivery's own docstring names.
                logger.exception("webhook_delivery_failed_unexpectedly")
                break
        if status is None:
            break
        sent += 1

    return {"fanned": fanned, "sent": sent}


async def run_forever(
    session_factory,
    keyring: KeyRing,
    *,
    interval_seconds: float = 10.0,
    batch_size: int = 25,
    stop: asyncio.Event | None = None,
) -> None:
    stop = stop or asyncio.Event()
    logger.info("webhook_dispatch_started", extra={"interval": interval_seconds})

    while not stop.is_set():
        started = dt.datetime.now(dt.UTC)
        try:
            stats = await run_once(session_factory, keyring, batch_size=batch_size)
            if stats["fanned"] or stats["sent"]:
                logger.info("webhook_dispatch_pass", extra=stats)
        except Exception:
            logger.exception("webhook_dispatch_pass_failed")

        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.5, interval_seconds - elapsed))

    logger.info("webhook_dispatch_stopped")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && TEST_POSTGRES_DSN="$TEST_POSTGRES_DSN" python -m pytest tests/test_webhook_dispatch_loop.py -v`
Expected: 2 passed.

- [ ] **Step 6: Wire both loops into `main.py`**

Edit `backend/notification_worker/app/main.py` — replace the whole file:

```python
"""Notification worker entrypoint.

Runs two independent polling loops as one container: alert dispatch (worker.py, unchanged)
and automatic webhook delivery (webhook_dispatch.py, new). Sharing one container rather
than adding a second is deliberate — CLAUDE.md's own resource math for the production box
(16 cores, no GPU) already treats "reuse infra, don't proliferate containers" as the
default, and both loops already need the exact same platform database role for the exact
same reason ("dispatch legitimately spans every tenant"). One shared `stop` event means a
single SIGTERM cleanly drains both.
"""
from __future__ import annotations

import asyncio

from app import webhook_dispatch
from app.worker import install_signal_handlers, run_forever
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.logging import configure_logging, get_logger
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.security.envelope import keyring_from_settings
from csense_shared.storage.objects import create_client

logger = get_logger(__name__)


async def amain() -> None:
    settings = get_settings()
    configure_logging("notification-worker", settings.environment, settings.log_level)

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    registry = build_registry(settings)
    if not registry.available_channels():
        # Not fatal. The worker still cancels superseded deliveries and records permanent
        # failures with a clear reason, which is more useful than refusing to start - and
        # it means a deployment with only email configured behaves correctly.
        logger.error(
            "no_notification_providers_configured",
            extra={"detail": "Set RESEND_API_KEY and/or WHATSAPP_GATEWAY_URL."},
        )

    try:
        object_store = create_client(settings)
    except Exception:  # noqa: BLE001 - snapshots are optional; alerts are not
        logger.exception("object_store_unavailable_attachments_disabled")
        object_store = None

    keyring = keyring_from_settings(settings)

    stop = asyncio.Event()
    install_signal_handlers(stop)

    try:
        await asyncio.gather(
            run_forever(
                session_factory,
                registry,
                object_store=object_store,
                interval_seconds=settings.notification_poll_seconds,
                batch_size=settings.notification_batch_size,
                stop=stop,
            ),
            webhook_dispatch.run_forever(
                session_factory,
                keyring,
                interval_seconds=settings.webhook_dispatch_poll_seconds,
                batch_size=settings.webhook_dispatch_batch_size,
                stop=stop,
            ),
        )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run the full backend suite and ruff**

Run: `cd backend && python -m pytest -q`
Expected: all previously-passing tests pass; new tests from this task pass.

Run: `cd backend && ruff check shared notification_worker tests`
Expected: no errors.

- [ ] **Step 8: Rebuild and confirm the container actually starts clean**

Run:
```bash
cd infra && docker compose --env-file ../.env up -d --build notification-worker
docker compose --env-file ../.env logs notification-worker --tail=50
```
Expected: `webhook_dispatch_started` and the pre-existing notification-worker startup log
both appear; no traceback; the container stays up (`docker compose ps` shows it running,
not restarting).

- [ ] **Step 9: Commit**

```bash
git add backend/shared/csense_shared/config.py backend/notification_worker/app/webhook_dispatch.py \
        backend/notification_worker/app/main.py backend/tests/test_webhook_dispatch_loop.py
git commit -m "Run automatic webhook delivery as a second loop inside notification-worker

fan_out_due_outbox_events + claim_and_send_one_delivery, polled every
webhook_dispatch_poll_seconds (default 10s), alongside the existing notification
dispatch loop in the same container - one shared stop event, one docker-compose
service, the same platform DB role already deployed for exactly this
cross-tenant-dispatch reason.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Real e2e script against the live stack

Per `CLAUDE.md`'s "verify for real, not by inspection" discipline, and mirroring
`scripts/e2e_webhooks.py`'s own precedent of proving delivery against a real public
endpoint (`httpbin.org`) rather than a mock.

**Files:**
- Create: `scripts/e2e_webhook_dispatch.py`
- Read first: `scripts/e2e_webhooks.py`

- [ ] **Step 1: Read the existing webhook e2e script for conventions to reuse**

Run: `cat scripts/e2e_webhooks.py` and note its login/tenant-setup helpers, base URLs, its
`psql()` helper, and its `httpbin.org` usage.

- [ ] **Step 2: Write the script**

Create `scripts/e2e_webhook_dispatch.py`. It must prove, against the live stack (which now
includes the rebuilt `notification-worker` container from Task 3):

1. A real tenant + logged-in customer user (reuse existing seeding helpers).
2. Create a real webhook endpoint (`POST /api/v1/tenant/webhooks`) pointed at
   `https://httpbin.org/post`, with `event_filters` naming the exact real `event_type` an
   incident-creation flow actually emits — **confirm that exact string by reading the code
   that emits it** (`csense_shared/pipeline/incidents.py`'s own `record_audit_and_outbox`
   call, or wherever the incident outbox event is written) rather than guessing.
3. Trigger a real domain event that lands in `outbox_events` with that `event_type` — seed
   an incident the same way `scripts/e2e_exports.py` already does, then perform a real
   state transition through the API if that's what actually emits the event.
4. Poll (up to ~60 seconds, every 2s — the worker's default interval is 10s) the
   `webhook_deliveries` table via the script's own `psql()` helper (the same direct-DB
   check `scripts/e2e_support_grant_authorization.py` already established for a column no
   API exposes) until a row for that endpoint reaches `status = 'succeeded'` or the
   timeout is hit.
5. Assert the delivery row shows `status = 'succeeded'` and `response_status = 200`
   (httpbin echoes 200 for a normal POST).
6. Assert exactly **one** delivery row exists for that endpoint — no duplicate fan-out
   across the several worker passes that will have run during the poll window. This proves
   `processed_events` idempotency for real, not just in the unit tests.
7. Create a **second** webhook endpoint whose `event_filters` do **not** match the event,
   and confirm after the same wait window that it received **zero** deliveries — proves
   `event_filters` is honored end to end.
8. Clean up everything it created (tenant, org, users, endpoints, deliveries,
   `processed_events` rows for this run's events), matching the cleanup discipline every
   other `e2e_*.py` script in this repo already follows.
9. Print a clear `PASS`/`FAIL` summary per assertion and exit non-zero on any failure.

- [ ] **Step 3: Run it against the live stack**

Run:
```bash
cd infra && docker compose --env-file ../.env up -d --build
cd .. && python scripts/e2e_webhook_dispatch.py
```
Expected: every assertion prints `PASS`; script exits 0. If the delivery never reaches
`succeeded` within the timeout, check `docker compose logs notification-worker` for the
real failure reason before assuming the test itself is wrong.

- [ ] **Step 4: Run ruff**

Run: `ruff check backend scripts` (the exact command CI uses, from the repo root)
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/e2e_webhook_dispatch.py
git commit -m "e2e: prove automatic webhook delivery against the live stack

A real incident event fans out through the outbox, notification-worker's new loop
delivers it to a real httpbin.org endpoint, exactly one delivery row lands (proving
processed_events idempotency for real), and a non-matching event_filters endpoint
receives nothing. Full PASS against the running Docker stack.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Update `CHECKLIST.md`

**Files:**
- Modify: `CHECKLIST.md` (the webhook entry — locate it by searching for "Webhook signing,
  verification, replay protection" — and its `[ ]` "Deliberately deferred" sub-bullet)

- [ ] **Step 1: Flip the deferred sub-bullet and the parent item**

Change the parent line:
```
- [~] Webhook signing, verification, replay protection
```
to:
```
- [x] Webhook signing, verification, replay protection, automatic outbox-driven delivery
```

Replace the `- [ ] **Deliberately deferred, stated plainly**: ...` sub-bullet with a new
`- [x]` sub-bullet describing what shipped: `csense_shared.webhooks.dispatcher`'s two
passes (fan-out via `processed_events` rather than `outbox_events.published_at`, and why;
send via `FOR UPDATE SKIP LOCKED` with the backoff/abandon policy and the named
claim-and-send-in-one-transaction simplification vs. `notification_worker`'s own split),
that it runs as a second loop inside the existing `notification-worker` container (no new
service), and a pointer to `scripts/e2e_webhook_dispatch.py` as the real-stack proof —
matching the detail level and voice of this file's other completed sub-bullets.

- [ ] **Step 2: Commit**

```bash
git add CHECKLIST.md
git commit -m "CHECKLIST.md: mark webhook automatic delivery done

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Index `outbox_events(occurred_at, id)`

Added after Task 2, from its implementer's `EXPLAIN (ANALYZE, BUFFERS)` assessment of the
real fan-out query. Not a micro-optimisation — the reasoning is that this table has **no
retention or pruning anywhere** (confirmed against `ingest.py`, `realtime.py`, `models.py`,
migration 0001, and the e2e scripts), so it grows monotonically, and:

- The 24h cutoff added in Task 2 bounds the *result set*, not the *work*: with no index on
  `occurred_at`, Postgres must seq-scan the whole table to evaluate the cutoff, so cost
  tracks total table size rather than the one-day window. At a few thousand events/day
  that's ~1M rows/year (~250 MB), re-scanned every `webhook_dispatch_poll_seconds` (10s),
  usually to discover there is nothing new — wasted CPU and shared-buffer pressure on a
  16-core box that `CLAUDE.md` already shows is CPU-bound.
- `ORDER BY occurred_at LIMIT 25` currently sorts the entire in-window match set; a btree
  lets it stop after 25 rows instead.
- **The strongest reason is that this is not only the new consumer's query.**
  `tenant_api/app/api/realtime.py` already runs
  `WHERE aggregate_type = 'incident' AND (occurred_at, id) > (:since_at, :since_id) ORDER BY occurred_at, id LIMIT :limit`
  against the same unindexed table — and it polls *per open browser tab, per tenant*, a far
  hotter path than a 10-second worker loop. `ix_outbox_unpublished`
  (`next_attempt_at WHERE published_at IS NULL`) serves neither query. This is pre-existing
  debt that automatic delivery makes visible, and one index fixes both.

**Files:**
- Create: `backend/migrations/versions/0052_outbox_events_occurred_at_index.py`

- [ ] **Step 1: Confirm the current head**

Run: `cd backend && python -m alembic heads`
Expected: `0051 (head)`. If not, set `down_revision` to whatever head actually is.

- [ ] **Step 2: Write the migration**

Create `backend/migrations/versions/0052_outbox_events_occurred_at_index.py`. It creates
`ix_outbox_events_occurred_at` as a btree on `outbox_events (occurred_at, id)`, and its
docstring must carry the reasoning above — specifically that it serves **two** consumers
(the new webhook dispatcher's fan-out scan and `realtime.py`'s existing per-tab incident
tail), that `ix_outbox_unpublished` serves neither, and that an index postpones rather than
solves the underlying "this table has no retention policy at all" problem, which is named
here as a real, separate decision someone should make deliberately rather than a gap nobody
noticed. Follow the structure of any recent index-adding migration in
`backend/migrations/versions/` for the `op.create_index`/`op.drop_index` shape.

- [ ] **Step 3: Apply and verify it is actually used**

Run: `cd backend && python -m alembic upgrade head`
Expected: `Running upgrade 0051 -> 0052`.

Then confirm the planner really uses it — this is the point of the task, so verify rather
than assume:
```bash
docker exec csense-postgres-1 psql -U csense_app -d csense -c "EXPLAIN (ANALYZE) SELECT e.id FROM outbox_events e WHERE e.tenant_id IS NOT NULL AND e.occurred_at > now() - interval '24 hours' AND NOT EXISTS (SELECT 1 FROM processed_events p WHERE p.consumer_name = 'webhook_dispatcher' AND p.event_id = e.id) ORDER BY e.occurred_at LIMIT 25;"
```
Note honestly in your report what the planner actually chose. At this table's current tiny
row count Postgres may still legitimately prefer a seq scan (that is correct behaviour for
a small table, not a failed index) — if so, say that explicitly rather than claiming a win
the output doesn't show, and confirm the index exists via `\di ix_outbox_events_occurred_at`.

- [ ] **Step 4: Full suite + ruff, then commit**

Run: `cd backend && python -m pytest -q && ruff check backend scripts` (from repo root for ruff)

```bash
git add backend/migrations/versions/0052_outbox_events_occurred_at_index.py
git commit -m "Migration 0052: index outbox_events(occurred_at, id)

Serves two consumers, not one: automatic webhook delivery's fan-out scan (added this
pass) and realtime.py's existing per-tab incident tail, which has been running against
an unindexed, never-pruned table since it shipped. ix_outbox_unpublished
(next_attempt_at WHERE published_at IS NULL) serves neither. Names the underlying
retention question - outbox_events has no pruning anywhere - as a real decision still
to be made rather than a gap nobody noticed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
