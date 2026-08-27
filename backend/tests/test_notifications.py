"""Notification dispatch: escalation, cancellation, retry and delivery recording.

The behaviour that matters most here is that acknowledging an incident stops the
escalation ladder. If that breaks, the site manager gets a 3am call after the guard
already dealt with it, people stop trusting the alerts, and the product is finished
regardless of how good the detection is.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.notifications.dispatcher import (
    EscalationStep,
    PolicyDefinition,
    cancel_pending_for_incident,
    claim_due_deliveries,
    create_notification,
    load_recipients,
    send_delivery,
)
from csense_shared.notifications.providers import (
    Channel,
    DeliveryOutcome,
    Message,
    ProviderRegistry,
    SendResult,
    mask_recipient,
    redact,
)
from csense_shared.notifications.retry import policy_for, schedule_next_attempt

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

NOW = dt.datetime(2026, 8, 26, 2, 0, tzinfo=dt.UTC)


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


class FakeProvider:
    """Records what it was asked to send and returns a scripted outcome."""

    def __init__(self, channel: Channel, outcome: DeliveryOutcome, *, code: str = "fake") -> None:
        self._channel = channel
        self._outcome = outcome
        self._code = code
        self.sent: list[Message] = []

    @property
    def code(self) -> str:
        return self._code

    @property
    def channel(self) -> Channel:
        return self._channel

    def validate_recipient(self, recipient: str) -> bool:
        return "@" in recipient if self._channel is Channel.EMAIL else recipient.startswith("+")

    async def send(self, message: Message) -> SendResult:
        self.sent.append(message)
        if self._outcome is DeliveryOutcome.ACCEPTED:
            return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id=f"msg-{len(self.sent)}")
        return SendResult(
            self._outcome, failure_code="scripted_failure", failure_summary="scripted"
        )


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
            {"n": f"Notify Test {suffix}", "s": f"notify-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code) VALUES (:t,'S',:c) RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}"},
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'C',:c,'ready') RETURNING id"),
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
                 "CAST('[\"email\",\"whatsapp\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id},
        )

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": str(tenant_id)})
        yield {
            "session": session, "factory": factory, "tenant_id": tenant_id,
            "incident_id": incident_id, "group_id": group_id,
        }
        await session.rollback()

    await engine.dispose()


def step(level=0, delay=0, channels=(Channel.EMAIL,), groups=()):
    return EscalationStep(level=level, delay_seconds=delay, channels=tuple(channels),
                          recipient_group_ids=tuple(groups))


async def _create(ctx, *, level=0, delay=0, channels=(Channel.EMAIL,), now=NOW):
    recipients = await load_recipients(
        ctx["session"], tenant_id=ctx["tenant_id"], group_ids=(ctx["group_id"],)
    )
    return await create_notification(
        ctx["session"],
        tenant_id=ctx["tenant_id"],
        incident_id=ctx["incident_id"],
        policy_version_id=None,
        severity="high",
        step=step(level=level, delay=delay, channels=channels),
        subject="Intrusion at Loading Bay 2",
        body="A person entered the restricted dock.",
        recipients=recipients,
        now=now,
    )


# --- Recipients ----------------------------------------------------------------------

async def test_recipients_load_with_addresses(ctx):
    recipients = await load_recipients(
        ctx["session"], tenant_id=ctx["tenant_id"], group_ids=(ctx["group_id"],)
    )
    assert len(recipients) == 1
    assert recipients[0].email == "guard@example.com"
    assert recipients[0].phone_e164 == "+919876543210"
    assert set(recipients[0].channels) == {Channel.EMAIL, Channel.WHATSAPP}


async def test_recipient_channel_preference_is_respected(ctx):
    """A step offering email and WhatsApp must not email someone who only wants WhatsApp."""
    await ctx["session"].execute(
        text("UPDATE recipient_group_members SET channels = CAST('[\"whatsapp\"]' AS jsonb) "
             "WHERE tenant_id = :t"),
        {"t": ctx["tenant_id"]},
    )
    notification_id = await _create(ctx, channels=(Channel.EMAIL, Channel.WHATSAPP))

    channels = (await ctx["session"].execute(
        text("SELECT channel::text FROM notification_deliveries WHERE notification_id = :n"),
        {"n": notification_id},
    )).scalars().all()
    assert channels == ["whatsapp"]


async def test_unknown_channel_preference_is_skipped_not_fatal(ctx):
    """Preferences are tenant-editable JSONB. One bad value must not stop the alert."""
    await ctx["session"].execute(
        text("UPDATE recipient_group_members SET channels = CAST('[\"email\",\"telepathy\"]' AS jsonb) "
             "WHERE tenant_id = :t"),
        {"t": ctx["tenant_id"]},
    )
    recipients = await load_recipients(
        ctx["session"], tenant_id=ctx["tenant_id"], group_ids=(ctx["group_id"],)
    )
    assert recipients[0].channels == (Channel.EMAIL,)


# --- Creating notifications ------------------------------------------------------------

async def test_notification_and_deliveries_are_created(ctx):
    notification_id = await _create(ctx, channels=(Channel.EMAIL, Channel.WHATSAPP))
    assert notification_id is not None

    rows = (await ctx["session"].execute(
        text("SELECT channel::text, recipient_ref, status::text FROM notification_deliveries "
             "WHERE notification_id = :n ORDER BY channel::text"),
        {"n": notification_id},
    )).all()
    assert [r[0] for r in rows] == ["email", "whatsapp"]
    assert all(r[2] == "queued" for r in rows)


async def test_same_escalation_level_cannot_fire_twice(ctx):
    """A redelivered incident event must not escalate the same rung again."""
    first = await _create(ctx, level=0)
    second = await _create(ctx, level=0)
    assert first is not None
    assert second is None, "duplicate escalation level must be refused"

    count = (await ctx["session"].execute(
        text("SELECT count(*) FROM notifications WHERE incident_id = :i"),
        {"i": ctx["incident_id"]},
    )).scalar_one()
    assert count == 1


async def test_later_escalation_levels_are_separate_notifications(ctx):
    await _create(ctx, level=0, delay=0)
    await _create(ctx, level=1, delay=300)

    levels = (await ctx["session"].execute(
        text("SELECT escalation_level FROM notifications WHERE incident_id = :i ORDER BY escalation_level"),
        {"i": ctx["incident_id"]},
    )).scalars().all()
    assert levels == [0, 1]


async def test_escalation_delay_sets_a_future_schedule(ctx):
    await _create(ctx, level=1, delay=600, now=NOW)
    scheduled = (await ctx["session"].execute(
        text("SELECT scheduled_at FROM notifications WHERE incident_id = :i AND escalation_level = 1"),
        {"i": ctx["incident_id"]},
    )).scalar_one()
    assert scheduled == NOW + dt.timedelta(seconds=600)


# --- Acknowledgement cancels escalation -------------------------------------------------

async def test_acknowledgement_cancels_pending_escalation(ctx):
    """The behaviour the whole alerting chain depends on: acknowledging at 02:03 must stop
    the 02:05 call to the site manager."""
    await _create(ctx, level=0, delay=0)
    await _create(ctx, level=1, delay=300)

    cancelled = await cancel_pending_for_incident(
        ctx["session"], tenant_id=ctx["tenant_id"], incident_id=ctx["incident_id"],
        reason="Acknowledged by guard", now=NOW,
    )
    assert cancelled == 2

    statuses = (await ctx["session"].execute(
        text("SELECT status::text FROM notifications WHERE incident_id = :i"),
        {"i": ctx["incident_id"]},
    )).scalars().all()
    assert set(statuses) == {"cancelled"}

    delivery_statuses = (await ctx["session"].execute(
        text("SELECT status::text FROM notification_deliveries WHERE tenant_id = :t"),
        {"t": ctx["tenant_id"]},
    )).scalars().all()
    assert set(delivery_statuses) == {"abandoned"}


async def test_cancellation_does_not_rewrite_already_sent_notifications(ctx):
    """A sent message cannot be unsent. Marking it cancelled would misreport history."""
    notification_id = await _create(ctx, level=0)
    await ctx["session"].execute(
        text("UPDATE notifications SET status = 'sent' WHERE id = :n"), {"n": notification_id}
    )

    cancelled = await cancel_pending_for_incident(
        ctx["session"], tenant_id=ctx["tenant_id"], incident_id=ctx["incident_id"],
        reason="Acknowledged", now=NOW,
    )
    assert cancelled == 0

    status = (await ctx["session"].execute(
        text("SELECT status::text FROM notifications WHERE id = :n"), {"n": notification_id}
    )).scalar_one()
    assert status == "sent"


# --- Sending -----------------------------------------------------------------------------

async def test_successful_send_records_acceptance(ctx):
    await _create(ctx, channels=(Channel.EMAIL,))
    registry = ProviderRegistry()
    provider = FakeProvider(Channel.EMAIL, DeliveryOutcome.ACCEPTED, code="resend")
    registry.register(provider)

    due = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    assert len(due) == 1

    status = await send_delivery(
        ctx["session"], registry, due[0], subject="Intrusion", body="A person entered.",
        now=NOW + dt.timedelta(seconds=1),
    )
    assert status == "accepted"
    assert len(provider.sent) == 1

    row = (await ctx["session"].execute(
        text("SELECT status::text, provider_code, provider_message_id, attempt_count, accepted_at "
             "FROM notification_deliveries WHERE id = :d"),
        {"d": due[0]["delivery_id"]},
    )).first()
    assert row[0] == "accepted"
    assert row[1] == "resend"
    assert row[2] is not None
    assert row[3] == 1
    assert row[4] is not None


async def test_retryable_failure_is_rescheduled(ctx):
    await _create(ctx, channels=(Channel.EMAIL,))
    registry = ProviderRegistry()
    registry.register(FakeProvider(Channel.EMAIL, DeliveryOutcome.FAILED_RETRYABLE))

    due = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    await send_delivery(ctx["session"], registry, due[0], subject="s", body="b",
                        now=NOW + dt.timedelta(seconds=1))

    row = (await ctx["session"].execute(
        text("SELECT status::text, attempt_count, next_attempt_at, failure_code "
             "FROM notification_deliveries WHERE id = :d"),
        {"d": due[0]["delivery_id"]},
    )).first()
    # Back in the queue with a future attempt time, not abandoned.
    assert row[0] == "queued"
    assert row[1] == 1
    assert row[2] > NOW
    assert row[3] == "scripted_failure"


async def test_permanent_failure_is_abandoned_immediately(ctx):
    """Retrying an invalid address forever hides a bad recipient behind a queue that
    never drains."""
    await _create(ctx, channels=(Channel.EMAIL,))
    registry = ProviderRegistry()
    registry.register(FakeProvider(Channel.EMAIL, DeliveryOutcome.FAILED_PERMANENT))

    due = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    status = await send_delivery(ctx["session"], registry, due[0], subject="s", body="b",
                                 now=NOW + dt.timedelta(seconds=1))

    assert status == "abandoned"
    row = (await ctx["session"].execute(
        text("SELECT status::text, next_attempt_at FROM notification_deliveries WHERE id = :d"),
        {"d": due[0]["delivery_id"]},
    )).first()
    assert row[0] == "abandoned"
    assert row[1] is None


async def test_unconfigured_channel_fails_clearly_rather_than_crashing(ctx):
    """A deployment without WhatsApp configured must produce a readable failure, not an
    exception that kills the worker mid-queue."""
    await _create(ctx, channels=(Channel.WHATSAPP,))
    registry = ProviderRegistry()  # nothing registered

    due = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    status = await send_delivery(ctx["session"], registry, due[0], subject="s", body="b",
                                 now=NOW + dt.timedelta(seconds=1))

    assert status == "abandoned"
    failure = (await ctx["session"].execute(
        text("SELECT failure_code FROM notification_deliveries WHERE id = :d"),
        {"d": due[0]["delivery_id"]},
    )).scalar_one()
    assert failure == "channel_not_configured"


async def test_cancelled_notifications_are_not_claimed(ctx):
    """Once acknowledged, nothing further should go out."""
    await _create(ctx, channels=(Channel.EMAIL,))
    await cancel_pending_for_incident(
        ctx["session"], tenant_id=ctx["tenant_id"], incident_id=ctx["incident_id"],
        reason="Acknowledged", now=NOW,
    )
    due = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=60))
    assert due == []


async def test_claiming_marks_rows_sending_so_two_workers_do_not_collide(ctx):
    await _create(ctx, channels=(Channel.EMAIL,))
    first = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    assert len(first) == 1
    # A second claim in the same transaction sees it already moved out of 'queued'.
    second = await claim_due_deliveries(ctx["session"], now=NOW + dt.timedelta(seconds=1))
    assert second == []


# --- Retry policy ------------------------------------------------------------------------

def test_whatsapp_gives_up_sooner_than_email():
    """A late intrusion alert is worse than useless; a late email is still useful."""
    assert policy_for(Channel.WHATSAPP).max_attempts < policy_for(Channel.EMAIL).max_attempts


def test_backoff_grows_and_is_capped():
    policy = policy_for(Channel.EMAIL)
    rng = random.Random(7)
    delays = [policy.next_delay(a, rng=rng).total_seconds() for a in range(1, 6)]
    assert delays[0] < delays[-1]
    assert max(delays) <= policy.max_delay_seconds * (1 + policy.jitter_ratio)


def test_jitter_spreads_simultaneous_retries():
    """Every camera on a site fires at once, so their retries would otherwise land
    together and rate-limit each other into a sustained outage."""
    policy = policy_for(Channel.EMAIL)
    rng = random.Random(3)
    delays = {policy.next_delay(2, rng=rng).total_seconds() for _ in range(20)}
    assert len(delays) > 10, "retries must not all be scheduled for the same instant"


def test_exhausted_policy_returns_no_next_attempt():
    assert schedule_next_attempt(Channel.WHATSAPP, policy_for(Channel.WHATSAPP).max_attempts) is None


# --- Redaction ----------------------------------------------------------------------------

def test_provider_errors_are_redacted_before_storage():
    """Provider error bodies routinely echo the recipient address and sometimes the key
    that was rejected. Neither belongs in a column support staff can read."""
    # Fabricated, and deliberately shaped like a real key: redaction that only catches
    # obviously-fake strings would prove nothing. The secret scanner is told about this
    # exact literal in .gitleaks.toml - by value, not by file, so any other key-shaped
    # string here is still reported.
    raw = "550 rejected for guard@example.com token Bearer sk_live_9f8a7b6c5d4e3f2a1b"
    cleaned = redact(raw)
    assert "guard@example.com" not in cleaned
    assert "sk_live_9f8a7b6c5d4e3f2a1b" not in cleaned
    assert "550 rejected" in cleaned


def test_phone_numbers_are_redacted():
    assert "+919876543210" not in redact("failed to deliver to +919876543210")


def test_masked_recipient_is_recognisable_but_not_harvestable():
    masked = mask_recipient("operations@northwind.example")
    assert masked.startswith("op") and masked.endswith("@northwind.example")
    assert "operations" not in masked


def test_policy_definition_digest_is_stable_and_content_addressed():
    a = PolicyDefinition(steps=(step(0, 0, (Channel.EMAIL,)),), severities=frozenset({"high"}))
    b = PolicyDefinition(steps=(step(0, 0, (Channel.EMAIL,)),), severities=frozenset({"high"}))
    c = PolicyDefinition(steps=(step(0, 60, (Channel.EMAIL,)),), severities=frozenset({"high"}))
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()


def test_policy_severity_filter():
    policy = PolicyDefinition(steps=(), severities=frozenset({"critical", "high"}))
    assert policy.applies_to("high")
    assert not policy.applies_to("low")
    # No severities means every severity.
    assert PolicyDefinition(steps=()).applies_to("info")
