"""The worker loop: claiming, cancelling, recovering and sending.

Two behaviours here are the ones that decide whether people keep trusting the alerts:

  **Acknowledging stops the ladder, including mid-flight.** Someone can acknowledge in the
  seconds between a delivery being claimed and the provider being called. The worker
  re-checks at send time, so that window does not produce the 3am call the product exists
  to prevent.

  **A worker that dies mid-send does not lose the alert.** Claiming marks rows `sending`
  and commits before any provider call, which is what lets several workers share the
  queue - and is also what strands rows if a worker is killed. The stalled sweep is the
  other half of that trade, and without it those alerts are silently never sent.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.notifications.dispatcher import (
    EscalationStep,
    create_notification,
    load_recipients,
)
from csense_shared.notifications.providers import (
    Channel,
    DeliveryOutcome,
    Message,
    ProviderRegistry,
    SendResult,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _load_worker():
    """Loads the worker module by path.

    Each service names its package `app`, so a plain `from app.worker import ...` would
    resolve to whichever service happened to be imported first - a test that silently
    exercises the wrong code. Loading by file path removes the ambiguity entirely.
    """
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "notification_worker"
        / "app"
        / "worker.py"
    )
    spec = importlib.util.spec_from_file_location("csense_notification_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_worker = _load_worker()
STALLED_AFTER = _worker.STALLED_AFTER
process_delivery = _worker.process_delivery
requeue_stalled_deliveries = _worker.requeue_stalled_deliveries
run_once = _worker.run_once

NOW = dt.datetime(2026, 8, 26, 2, 0, tzinfo=dt.UTC)


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


class RecordingProvider:
    def __init__(self, channel: Channel = Channel.EMAIL, outcome=DeliveryOutcome.ACCEPTED):
        self._channel = channel
        self._outcome = outcome
        self.sent: list[Message] = []

    @property
    def code(self) -> str:
        return "recording"

    @property
    def channel(self) -> Channel:
        return self._channel

    def validate_recipient(self, recipient: str) -> bool:
        return "@" in recipient

    async def send(self, message: Message) -> SendResult:
        self.sent.append(message)
        if self._outcome is DeliveryOutcome.ACCEPTED:
            return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id="rec-1")
        return SendResult(self._outcome, failure_code="scripted", failure_summary="scripted")


@pytest_asyncio.fixture()
async def ctx():
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                 "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Worker Test {suffix}", "s": f"worker-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code) VALUES (:t,'Depot',:c) RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}"},
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Bay 2',:c,'ready') RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )).scalar_one()
        incident_id = (await session.execute(
            text("INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, type_code, "
                 "severity, title, first_detected_at, last_detected_at) "
                 "VALUES (:t,:s,:c,1,'zone.intrusion','high','Intrusion',:n,:n) RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": camera_id, "n": NOW},
        )).scalar_one()
        group_id = (await session.execute(
            text("INSERT INTO recipient_groups (tenant_id, name) VALUES (:t,'Night shift') RETURNING id"),
            {"t": tenant_id},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO recipient_group_members "
                 "(tenant_id, recipient_group_id, display_name, email, phone_e164, channels) "
                 "VALUES (:t,:g,'Guard','guard@example.com','+919876543210', "
                 "CAST('[\"email\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id},
        )

        # `run_once` deliberately drains every tenant - that is what the worker does in
        # production. On a shared test database that means anything another test left
        # queued would be claimed here and counted in this test's stats. Quiescing the
        # queue first makes each test's numbers describe only its own work.
        await session.execute(
            text(
                "UPDATE notification_deliveries SET status = 'cancelled', "
                "next_attempt_at = NULL WHERE status IN ('queued','sending')"
            )
        )

    yield {
        "factory": factory, "tenant_id": tenant_id, "incident_id": incident_id,
        "group_id": group_id,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        # Tenants do not cascade from organizations, so the tenant goes first; everything
        # below it (sites, cameras, incidents, notifications) does cascade from the tenant.
        await session.execute(
            text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
        )
    await engine.dispose()


async def _queue_notification(ctx, *, delay: int = 0, now: dt.datetime = NOW) -> uuid.UUID:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        recipients = await load_recipients(
            session, tenant_id=ctx["tenant_id"], group_ids=(ctx["group_id"],)
        )
        return await create_notification(
            session,
            tenant_id=ctx["tenant_id"],
            incident_id=ctx["incident_id"],
            policy_version_id=None,
            severity="high",
            step=EscalationStep(
                level=0, delay_seconds=delay, channels=(Channel.EMAIL,),
                recipient_group_ids=(ctx["group_id"],),
            ),
            subject="Intrusion at Depot",
            body="A person entered the restricted dock.",
            recipients=recipients,
            now=now,
        )


async def _delivery_status(ctx, notification_id) -> list[str]:
    async with ctx["factory"]() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return (await session.execute(
            text("SELECT status::text FROM notification_deliveries WHERE notification_id = :n"),
            {"n": notification_id},
        )).scalars().all()


async def _set_incident_status(ctx, status: str) -> None:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE incidents SET status = CAST(:s AS incident_status) WHERE id = :i"),
            {"s": status, "i": ctx["incident_id"]},
        )


# --- The happy path -------------------------------------------------------------------

async def test_due_delivery_is_sent_and_recorded(ctx):
    notification_id = await _queue_notification(ctx)
    provider = RecordingProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    stats = await run_once(ctx["factory"], registry, now=NOW)

    assert stats.claimed == 1
    assert stats.accepted == 1
    assert await _delivery_status(ctx, notification_id) == ["accepted"]
    assert provider.sent[0].recipient == "guard@example.com"
    assert "restricted dock" in provider.sent[0].body


async def test_delivery_scheduled_for_later_is_not_claimed(ctx):
    """An escalation rung ten minutes out must not fire immediately."""
    notification_id = await _queue_notification(ctx, delay=600)
    registry = ProviderRegistry()
    registry.register(RecordingProvider())

    stats = await run_once(ctx["factory"], registry, now=NOW)

    assert stats.claimed == 0
    assert await _delivery_status(ctx, notification_id) == ["queued"]

    # ...and is claimed once its time arrives.
    stats = await run_once(ctx["factory"], registry, now=NOW + dt.timedelta(seconds=601))
    assert stats.accepted == 1


# --- Acknowledgement stops the ladder -------------------------------------------------

@pytest.mark.parametrize(
    "status", ["acknowledged", "investigating", "escalated", "resolved", "dismissed"]
)
async def test_handled_incident_is_never_sent(ctx, status):
    """Every one of these means a human saw it. None should page anybody."""
    notification_id = await _queue_notification(ctx)
    await _set_incident_status(ctx, status)

    provider = RecordingProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    stats = await run_once(ctx["factory"], registry, now=NOW)

    assert stats.skipped == 1
    assert provider.sent == []
    assert await _delivery_status(ctx, notification_id) == ["cancelled"]


async def test_acknowledgement_between_claim_and_send_still_stops_it(ctx):
    """The window the re-check exists to close.

    The delivery is claimed while the incident is open, then acknowledged before
    `process_delivery` runs - exactly what happens when a guard acts while the worker is
    mid-pass. Checking only at claim time would send anyway.
    """
    await _queue_notification(ctx)

    from csense_shared.notifications.dispatcher import claim_due_deliveries

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        claimed = await claim_due_deliveries(session, limit=10, now=NOW)
    assert len(claimed) == 1

    await _set_incident_status(ctx, "acknowledged")

    provider = RecordingProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        status = await process_delivery(session, registry, claimed[0], now=NOW)

    assert status == "cancelled"
    assert provider.sent == []


# --- Recovery -------------------------------------------------------------------------

async def test_stalled_delivery_is_requeued(ctx):
    """A worker killed mid-send leaves rows nobody would ever claim again."""
    notification_id = await _queue_notification(ctx)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE notification_deliveries SET status='sending', updated_at = :old "
                 "WHERE notification_id = :n"),
            {"n": notification_id, "old": NOW - STALLED_AFTER - dt.timedelta(minutes=1)},
        )

    registry = ProviderRegistry()
    registry.register(RecordingProvider())
    stats = await run_once(ctx["factory"], registry, now=NOW)

    assert stats.requeued == 1
    assert stats.accepted == 1


async def test_a_merely_slow_send_is_not_requeued_underneath_itself(ctx):
    """Requeueing too eagerly delivers the same alert twice."""
    notification_id = await _queue_notification(ctx)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE notification_deliveries SET status='sending', updated_at = :recent "
                 "WHERE notification_id = :n"),
            {"n": notification_id, "recent": NOW - dt.timedelta(seconds=30)},
        )
        requeued = await requeue_stalled_deliveries(session, now=NOW)

    assert requeued == 0


async def test_requeue_does_not_burn_a_retry_attempt(ctx):
    """A worker restart is not a provider failure; charging it one exhausts the budget."""
    notification_id = await _queue_notification(ctx)

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(
            text("UPDATE notification_deliveries SET status='sending', attempt_count = 1, "
                 "updated_at = :old WHERE notification_id = :n"),
            {"n": notification_id, "old": NOW - STALLED_AFTER - dt.timedelta(minutes=1)},
        )
        await requeue_stalled_deliveries(session, now=NOW)

    async with ctx["factory"]() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        attempts = (await session.execute(
            text("SELECT attempt_count FROM notification_deliveries WHERE notification_id = :n"),
            {"n": notification_id},
        )).scalar_one()

    assert attempts == 1


# --- Failure handling -----------------------------------------------------------------

async def test_unconfigured_channel_fails_permanently_not_forever(ctx):
    """An empty registry must not create a queue that never drains."""
    notification_id = await _queue_notification(ctx)

    stats = await run_once(ctx["factory"], ProviderRegistry(), now=NOW)

    assert stats.failed == 1
    # `abandoned`, not `failed`: there is no retry that could ever succeed, so the row
    # reaches a terminal state immediately rather than sitting in a queue that never
    # drains. `cancelled` would be wrong too - nobody acknowledged anything here.
    assert await _delivery_status(ctx, notification_id) == ["abandoned"]


async def test_retryable_failure_is_rescheduled(ctx):
    notification_id = await _queue_notification(ctx)
    registry = ProviderRegistry()
    registry.register(RecordingProvider(outcome=DeliveryOutcome.FAILED_RETRYABLE))

    await run_once(ctx["factory"], registry, now=NOW)

    async with ctx["factory"]() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        row = (await session.execute(
            text("SELECT status::text, next_attempt_at FROM notification_deliveries "
                 "WHERE notification_id = :n"),
            {"n": notification_id},
        )).first()

    assert row[0] == "queued"
    assert row[1] > NOW
