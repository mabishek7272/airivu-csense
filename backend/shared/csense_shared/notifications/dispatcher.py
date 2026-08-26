"""Turning an incident into delivered notifications (TRD §18).

The flow: an incident fires, a policy version decides who to tell and how, a notification
row records the intent, and one delivery row per recipient-channel records each attempt.

Three properties this is built around:

**Acknowledgement cancels escalation.** The single most important behaviour in the whole
alerting chain. If a guard acknowledges an intrusion at 02:03, the site manager must not
be phoned at 02:05. `cancel_pending_for_incident` is called on acknowledgement and marks
every not-yet-sent notification cancelled.

**A failed channel escalates rather than retrying forever.** When a channel exhausts its
retries the delivery is `abandoned` and the ladder moves on. Retrying a dead WhatsApp
instance for an hour means nobody is told at all.

**Every attempt is a row.** `notification_deliveries` is the evidence that alerting works.
A silent system and a working one look identical until you query this table.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.notifications.providers import (
    Attachment,
    Channel,
    Message,
    ProviderRegistry,
    mask_recipient,
)
from csense_shared.notifications.retry import schedule_next_attempt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EscalationStep:
    """One rung of the ladder: who, how, and how long after the incident opened."""

    level: int
    delay_seconds: int
    channels: tuple[Channel, ...]
    recipient_group_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class PolicyDefinition:
    """The published, immutable content of a notification policy version."""

    steps: tuple[EscalationStep, ...]
    # Severities this policy applies to; empty means all.
    severities: frozenset[str] = field(default_factory=frozenset)

    def to_json(self) -> dict:
        return {
            "severities": sorted(self.severities),
            "steps": [
                {
                    "level": s.level,
                    "delay_seconds": s.delay_seconds,
                    "channels": [str(c) for c in s.channels],
                    "recipient_group_ids": [str(g) for g in s.recipient_group_ids],
                }
                for s in self.steps
            ],
        }

    def digest(self) -> str:
        """Content hash, so a version's definition can be proven unchanged."""
        canonical = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def applies_to(self, severity: str) -> bool:
        return not self.severities or severity in self.severities


@dataclass(frozen=True)
class Recipient:
    name: str | None
    email: str | None
    phone_e164: str | None
    channels: tuple[Channel, ...]

    def address_for(self, channel: Channel) -> str | None:
        if channel is Channel.EMAIL:
            return self.email
        if channel in (Channel.WHATSAPP, Channel.SMS):
            return self.phone_e164
        return None


_VALID_CHANNELS = {c.value for c in Channel}


def _parse_channels(raw: list | None) -> tuple[Channel, ...]:
    """Channel preferences come from JSONB a tenant can edit. An unknown value is skipped
    rather than raising - one bad preference must not stop the whole alert going out."""
    return tuple(Channel(c) for c in (raw or []) if c in _VALID_CHANNELS)


async def load_recipients(
    session: AsyncSession, *, tenant_id: uuid.UUID, group_ids: tuple[uuid.UUID, ...]
) -> list[Recipient]:
    """Members of the given groups, with a usable address.

    A member's own `channels` preference intersects with what the step asks for, so a
    recipient who only wants WhatsApp is not emailed by a step that offers both.
    """
    if not group_ids:
        return []

    rows = (
        await session.execute(
            text(
                """
                SELECT m.display_name, m.email, m.phone_e164, m.channels,
                       u.display_name AS user_name, u.email_display AS user_email
                FROM recipient_group_members m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.tenant_id = :tenant_id
                  AND m.recipient_group_id = ANY(:group_ids)
                  AND m.status = 'active'
                """
            ),
            {"tenant_id": tenant_id, "group_ids": list(group_ids)},
        )
    ).all()

    recipients = []
    for display_name, email, phone, channels, user_name, user_email in rows:
        recipients.append(
            Recipient(
                name=display_name or user_name,
                # A bare contact's own address wins; otherwise fall back to the linked
                # user's, so adding a colleague by user id "just works".
                email=email or user_email,
                phone_e164=phone,
                channels=_parse_channels(channels),
            )
        )
    return recipients


async def create_notification(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    policy_version_id: uuid.UUID | None,
    severity: str,
    step: EscalationStep,
    subject: str,
    body: str,
    recipients: list[Recipient],
    media_urls: list[str] | None = None,
    now: dt.datetime | None = None,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Creates the notification and its delivery rows for one escalation step.

    Returns None when this step already exists for the incident - the unique index on
    (tenant, incident, level) is what stops a redelivered event escalating twice.
    """
    moment = now or dt.datetime.now(dt.UTC)
    scheduled_at = moment + dt.timedelta(seconds=step.delay_seconds)

    notification_id = (
        await session.execute(
            text(
                """
                INSERT INTO notifications
                    (tenant_id, incident_id, policy_version_id, severity, escalation_level,
                     status, subject, body, scheduled_at, correlation_id)
                VALUES (:tenant_id, :incident_id, :policy_version_id, :severity, :level,
                        'scheduled', :subject, :body, :scheduled_at, :correlation_id)
                ON CONFLICT (tenant_id, incident_id, escalation_level)
                    WHERE incident_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "policy_version_id": policy_version_id,
                "severity": severity,
                "level": step.level,
                "subject": subject,
                "body": body,
                "scheduled_at": scheduled_at,
                "correlation_id": correlation_id,
            },
        )
    ).scalar_one_or_none()

    if notification_id is None:
        return None

    for recipient in recipients:
        for channel in step.channels:
            # Only channels this recipient actually accepts.
            if recipient.channels and channel not in recipient.channels:
                continue
            address = recipient.address_for(channel)
            if not address:
                continue
            await session.execute(
                text(
                    """
                    INSERT INTO notification_deliveries
                        (tenant_id, notification_id, channel, recipient_ref, recipient_name,
                         provider_code, status, next_attempt_at)
                    VALUES (:tenant_id, :notification_id, CAST(:channel AS notification_channel),
                            :recipient_ref, :recipient_name, :provider_code, 'queued', :next_attempt_at)
                    ON CONFLICT (notification_id, channel, recipient_ref) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "notification_id": notification_id,
                    "channel": str(channel),
                    "recipient_ref": address,
                    "recipient_name": recipient.name,
                    # Resolved at send time; recorded now so an unconfigured channel is
                    # visible as a queued delivery rather than a missing row.
                    "provider_code": "pending",
                    "next_attempt_at": scheduled_at,
                },
            )

    return notification_id


async def cancel_pending_for_incident(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    reason: str,
    now: dt.datetime | None = None,
) -> int:
    """Cancels notifications not yet sent for an incident.

    Called when someone acknowledges or resolves. This is what stops the escalation ladder
    waking the site manager after the guard already handled it - the single behaviour that
    determines whether people keep trusting the alerts.

    Already-sent notifications are untouched: they cannot be unsent, and pretending
    otherwise would misrepresent the history.
    """
    moment = now or dt.datetime.now(dt.UTC)
    result = await session.execute(
        text(
            """
            UPDATE notifications
            SET status = 'cancelled', cancelled_at = :now, cancelled_reason = :reason,
                updated_at = :now
            WHERE tenant_id = :tenant_id AND incident_id = :incident_id
              AND status IN ('pending', 'scheduled')
            RETURNING id
            """
        ),
        {"tenant_id": tenant_id, "incident_id": incident_id, "reason": reason, "now": moment},
    )
    cancelled = [row[0] for row in result.all()]

    if cancelled:
        await session.execute(
            text(
                """
                UPDATE notification_deliveries
                SET status = 'abandoned', next_attempt_at = NULL,
                    failure_code = 'cancelled', failure_summary_redacted = :reason,
                    updated_at = :now
                WHERE tenant_id = :tenant_id AND notification_id = ANY(:ids)
                  AND status IN ('queued', 'sending')
                """
            ),
            {"tenant_id": tenant_id, "ids": cancelled, "reason": reason[:200], "now": moment},
        )

    return len(cancelled)


async def claim_due_deliveries(
    session: AsyncSession, *, limit: int = 50, now: dt.datetime | None = None
) -> list[dict]:
    """Claims deliveries that are due, marking them `sending` in the same statement.

    `FOR UPDATE SKIP LOCKED` so several workers can drain the queue concurrently without
    any two claiming the same row - and without a worker that dies mid-send blocking the
    others behind a held lock.

    Runs platform-scoped across all tenants, so the caller needs a platform session.
    """
    moment = now or dt.datetime.now(dt.UTC)
    rows = (
        await session.execute(
            text(
                """
                WITH due AS (
                    SELECT d.id
                    FROM notification_deliveries d
                    JOIN notifications n ON n.id = d.notification_id
                    WHERE d.status = 'queued'
                      AND d.next_attempt_at <= :now
                      AND n.status NOT IN ('cancelled', 'failed')
                    ORDER BY d.next_attempt_at
                    LIMIT :limit
                    FOR UPDATE OF d SKIP LOCKED
                )
                UPDATE notification_deliveries d
                SET status = 'sending', updated_at = :now
                FROM due
                WHERE d.id = due.id
                RETURNING d.id, d.tenant_id, d.notification_id, d.channel::text,
                          d.recipient_ref, d.recipient_name, d.attempt_count
                """
            ),
            {"now": moment, "limit": limit},
        )
    ).all()

    return [
        {
            "delivery_id": r[0],
            "tenant_id": r[1],
            "notification_id": r[2],
            "channel": Channel(r[3]),
            "recipient_ref": r[4],
            "recipient_name": r[5],
            "attempt_count": r[6],
        }
        for r in rows
    ]


async def send_delivery(
    session: AsyncSession,
    registry: ProviderRegistry,
    delivery: dict,
    *,
    subject: str | None,
    body: str,
    media_urls: list[str] | None = None,
    attachments: list[Attachment] | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Sends one delivery and records the outcome. Returns the resulting status.

    Media arrives two ways because the channels consume it differently: `media_urls` for
    providers whose own server fetches the bytes (the WhatsApp gateway, from inside our
    network), `attachments` for providers that must carry them (email, since Gmail proxies
    `<img src>` through Google's fetchers and would need a publicly reachable URL).
    """
    moment = now or dt.datetime.now(dt.UTC)
    channel: Channel = delivery["channel"]
    attempt = delivery["attempt_count"] + 1

    provider = registry.get(channel)
    if provider is None:
        # A channel with no configured provider is a permanent failure with a clear
        # reason, not a crash and not an endless retry.
        return await _record_failure(
            session, delivery, attempt=attempt, now=moment,
            failure_code="channel_not_configured",
            failure_summary=f"No provider is configured for the {channel} channel.",
            retryable=False, provider_code="none",
        )

    if not provider.validate_recipient(delivery["recipient_ref"]):
        return await _record_failure(
            session, delivery, attempt=attempt, now=moment,
            failure_code="invalid_recipient",
            failure_summary=f"{mask_recipient(delivery['recipient_ref'])} is not valid for {channel}.",
            retryable=False, provider_code=provider.code,
        )

    message = Message(
        recipient=delivery["recipient_ref"],
        subject=subject,
        body=body,
        media_urls=media_urls or [],
        attachments=attachments or [],
        # Stable per delivery+attempt-window, so a provider that honours it will not
        # double-send if our own write fails after their accept.
        idempotency_key=f"{delivery['delivery_id']}",
        metadata={"delivery": str(delivery["delivery_id"])[:32]},
    )

    result = await provider.send(message)

    if result.accepted:
        await session.execute(
            text(
                """
                UPDATE notification_deliveries
                SET status = 'accepted', attempt_count = :attempt, provider_code = :provider,
                    provider_message_id = :message_id, accepted_at = :now,
                    next_attempt_at = NULL, failure_code = NULL,
                    failure_summary_redacted = NULL, updated_at = :now
                WHERE id = :id
                """
            ),
            {
                "id": delivery["delivery_id"],
                "attempt": attempt,
                "provider": provider.code,
                "message_id": result.provider_message_id,
                "now": moment,
            },
        )
        return "accepted"

    return await _record_failure(
        session, delivery, attempt=attempt, now=moment,
        failure_code=result.failure_code or "unknown",
        failure_summary=result.failure_summary,
        retryable=result.should_retry, provider_code=provider.code,
    )


async def _record_failure(
    session: AsyncSession,
    delivery: dict,
    *,
    attempt: int,
    now: dt.datetime,
    failure_code: str,
    failure_summary: str | None,
    retryable: bool,
    provider_code: str,
) -> str:
    next_attempt = (
        schedule_next_attempt(delivery["channel"], attempt, now=now) if retryable else None
    )
    # `failed` means "will try again"; `abandoned` means "this channel is done".
    # The distinction is what tells an operator whether the queue is working or stuck.
    status = "failed" if next_attempt else "abandoned"

    await session.execute(
        text(
            """
            UPDATE notification_deliveries
            SET status = CAST(:status AS delivery_status), attempt_count = :attempt,
                provider_code = :provider, failure_code = :failure_code,
                failure_summary_redacted = :summary, failed_at = :now,
                next_attempt_at = :next_attempt, updated_at = :now
            WHERE id = :id
            """
        ),
        {
            "id": delivery["delivery_id"],
            "status": "queued" if next_attempt else "abandoned",
            "attempt": attempt,
            "provider": provider_code,
            "failure_code": failure_code,
            "summary": failure_summary,
            "now": now,
            "next_attempt": next_attempt,
        },
    )

    if not next_attempt:
        logger.warning(
            "notification_delivery_abandoned",
            extra={
                "delivery_id": str(delivery["delivery_id"]),
                "channel": str(delivery["channel"]),
                "failure_code": failure_code,
                "attempts": attempt,
            },
        )
    return status
