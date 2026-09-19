"""The loop that actually sends alerts.

Everything else in the notification stack decides *what* should be sent. Nothing drove it
until this service existed, which meant an incident could be detected, an escalation
ladder written, and not one message ever leave the building.

Three design points worth stating, because each has a failure mode that only shows up in
production:

**One database transaction per delivery, not per batch.** A batch transaction means one
provider timeout at message 40 rolls back the 39 sends already recorded - and since the
provider really did send them, the retry duplicates every one. Per-delivery commits make a
crash cost at most one ambiguous message, which the provider idempotency key then covers.

**Claim and send are separated.** `claim_due_deliveries` marks rows `sending` under
`FOR UPDATE SKIP LOCKED` and commits immediately, so several workers can drain the queue
together and a slow provider call never holds a row lock. The cost is that a worker killed
mid-send leaves a row stuck in `sending`; `requeue_stalled_deliveries` is what recovers
those, and it is the reason the service can be restarted without losing alerts.

**Acknowledgement is checked at send time, not only at claim time.** Someone can
acknowledge an incident in the seconds between the two. Sending anyway is exactly the 3am
call the product exists to prevent, so the check is repeated immediately before the
provider call.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import signal
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.config import get_settings
from csense_shared.notifications.dispatcher import claim_due_deliveries, send_delivery
from csense_shared.notifications.providers import Attachment, Channel, ProviderRegistry
from csense_shared.pipeline.incidents import STOPS_ESCALATION
from csense_shared.storage.objects import public_branding_url

logger = logging.getLogger(__name__)

# How long a row may sit in `sending` before it is presumed abandoned. Comfortably longer
# than the slowest provider timeout (WhatsApp media at 20s), so a merely slow send is
# never requeued underneath itself and delivered twice.
STALLED_AFTER = dt.timedelta(minutes=5)

# Attachments are capped well below Resend's 40MB limit; a handful of snapshots is enough
# to show what happened, and more would push the mail into spam folders.
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024


class WorkerStats:
    def __init__(self) -> None:
        self.claimed = 0
        self.accepted = 0
        self.failed = 0
        self.skipped = 0
        self.requeued = 0

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "accepted": self.accepted,
            "failed": self.failed,
            "skipped": self.skipped,
            "requeued": self.requeued,
        }


async def requeue_stalled_deliveries(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> int:
    """Returns deliveries abandoned by a dead worker to the queue.

    Without this, a worker killed mid-send leaves rows in `sending` that no other worker
    will ever claim - alerts that were promised and silently never sent. Attempt count is
    deliberately not incremented: the row is being recovered, not retried after a real
    provider failure, and burning a retry budget for a worker restart would eventually
    exhaust it.
    """
    moment = now or dt.datetime.now(dt.UTC)
    result = await session.execute(
        text(
            """
            UPDATE notification_deliveries
            SET status = 'queued', next_attempt_at = :now, updated_at = :now
            WHERE status = 'sending' AND updated_at < :cutoff
            """
        ),
        {"now": moment, "cutoff": moment - STALLED_AFTER},
    )
    return result.rowcount or 0


async def load_notification_content(
    session: AsyncSession, notification_id: uuid.UUID
) -> dict | None:
    """Subject, body, and whether the incident has since been handled."""
    row = (
        await session.execute(
            text(
                """
                SELECT n.subject, n.body, n.status::text, n.incident_id, n.escalation_level,
                       i.status::text
                FROM notifications n
                LEFT JOIN incidents i ON i.id = n.incident_id
                WHERE n.id = :id
                """
            ),
            {"id": notification_id},
        )
    ).first()
    if row is None:
        return None
    return {
        "subject": row[0],
        "body": row[1] or "",
        "notification_status": row[2],
        "incident_id": row[3],
        "escalation_level": row[4],
        "incident_status": row[5],
    }


def _read_object(client, bucket: str, key: str) -> bytes:
    """Reads an object fully, always releasing the connection.

    MinIO's `get_object` hands back a live HTTP response. Without the release the
    connection stays checked out of the pool, and after enough alerts the worker stops
    being able to fetch anything at all - which looks like snapshots mysteriously
    disappearing from emails rather than a leak.
    """
    response = None
    try:
        response = client.get_object(bucket, key)
        return response.read()
    finally:
        if response is not None:
            response.close()
            response.release_conn()


async def load_attachments(
    session: AsyncSession, object_store, *, incident_id: uuid.UUID | None
) -> list[Attachment]:
    """Annotated snapshots for the incident, as bytes.

    Only the `annotated` variant is ever attached. It is drawn on the *masked* image, so
    faces stay blurred underneath their own boxes - an unmasked frame must not leave the
    system in an email, which is exactly the kind of thing that gets forwarded.

    Failure here is never fatal: an alert with no picture is still an alert.
    """
    if incident_id is None or object_store is None:
        return []

    rows = (
        await session.execute(
            text(
                """
                SELECT o.bucket, o.object_key, o.mime_type, o.size_bytes
                FROM evidence e
                JOIN stored_objects o ON o.id = e.object_id
                WHERE e.incident_id = :incident_id
                  AND e.privacy_variant = 'annotated'
                ORDER BY e.capture_time
                LIMIT :limit
                """
            ),
            {"incident_id": incident_id, "limit": MAX_ATTACHMENTS},
        )
    ).all()

    attachments: list[Attachment] = []
    for index, (bucket, key, mime_type, size_bytes) in enumerate(rows, start=1):
        if size_bytes and size_bytes > MAX_ATTACHMENT_BYTES:
            logger.info("attachment_too_large", extra={"key": key, "bytes": size_bytes})
            continue
        try:
            # MinIO's client is synchronous, so the fetch goes to a thread rather than
            # blocking the event loop and stalling every other delivery behind it.
            content = await asyncio.to_thread(_read_object, object_store, bucket, key)
        except Exception as exc:  # noqa: BLE001 - never lose the alert over a snapshot
            logger.warning(
                "attachment_fetch_failed", extra={"key": key, "error": str(exc)[:200]}
            )
            continue
        suffix = "jpg" if (mime_type or "").endswith("jpeg") else "png"
        attachments.append(
            Attachment(
                filename=f"snapshot-{index}.{suffix}",
                content=content,
                mime_type=mime_type or "image/jpeg",
            )
        )
    return attachments


async def load_media_urls(
    session: AsyncSession, object_store, *, incident_id: uuid.UUID | None
) -> list[str]:
    """A presigned URL for the incident's annotated snapshot - the counterpart to
    `load_attachments`, for channels whose own server fetches the bytes rather than
    carrying them (the WhatsApp gateway, from inside this Docker network).

    Both exist because the two channels genuinely need different things (see
    `providers.Message`'s own docstring) - this is not dead code, it was simply never
    written until a WhatsApp alert was actually checked for the snapshot the equivalent
    email already carries.

    Signed against `object_store` - the same internal, server-side client
    `load_attachments` reads bytes with - not the public endpoint a browser needs. The
    WhatsApp gateway is a container on this network, not a browser; a browser-signed URL
    would not resolve for it. Short-lived: it only has to survive the few seconds until
    the gateway fetches it, not sit around as a standing link to evidence.
    """
    if incident_id is None or object_store is None:
        return []

    row = (
        await session.execute(
            text(
                """
                SELECT o.bucket, o.object_key
                FROM evidence e
                JOIN stored_objects o ON o.id = e.object_id
                WHERE e.incident_id = :incident_id
                  AND e.privacy_variant = 'annotated'
                ORDER BY e.capture_time
                LIMIT 1
                """
            ),
            {"incident_id": incident_id},
        )
    ).first()
    if row is None:
        return []

    bucket, key = row
    try:
        url = await asyncio.to_thread(
            object_store.presigned_get_object, bucket, key, expires=dt.timedelta(minutes=10)
        )
    except Exception as exc:  # noqa: BLE001 - never lose the alert over a snapshot
        logger.warning("media_url_presign_failed", extra={"key": key, "error": str(exc)[:200]})
        return []
    return [url]


async def load_branding(
    session: AsyncSession, tenant_id: uuid.UUID | None
) -> tuple[str | None, str | None, str | None]:
    """White-label branding for an email delivery: `(from_name, logo_url, footer_text)`,
    all `None` for the default (unbranded) case. Same "must never lose the alert over
    this" discipline as `load_media_urls` above: best-effort, log-and-continue on any
    failure rather than letting a branding lookup block a real incident notification.

    `org_branding_resolve` (not `_resolve_by_slug`) - this already has a verified
    `tenant_id` from the claimed delivery row, never a client-supplied slug.
    """
    if tenant_id is None:
        return None, None, None
    try:
        organization_id = (
            await session.execute(
                text("SELECT organization_id FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        ).scalar_one_or_none()
        if organization_id is None:
            return None, None, None

        row = (
            await session.execute(
                text(
                    """
                    SELECT ob.display_name, ob.email_from_name, so.object_key
                    FROM org_branding_resolve(:organization_id) r
                    JOIN org_branding ob ON ob.organization_id = r.source_organization_id
                    LEFT JOIN stored_objects so ON so.id = r.logo_object_id
                    """
                ),
                {"organization_id": organization_id},
            )
        ).first()
        if row is None:
            return None, None, None

        display_name, email_from_name, object_key = row
        settings = get_settings()
        logo_url = public_branding_url(settings, object_key) if object_key else None
        footer_text = f"Sent by {display_name}."
        return email_from_name, logo_url, footer_text
    except Exception as exc:  # noqa: BLE001 - never lose the alert over branding
        logger.warning("branding_lookup_failed", extra={"tenant_id": str(tenant_id), "error": str(exc)[:200]})
        return None, None, None


async def process_delivery(
    session: AsyncSession,
    registry: ProviderRegistry,
    delivery: dict,
    *,
    object_store=None,
    now: dt.datetime | None = None,
) -> str:
    """Sends one claimed delivery. Returns the resulting status."""
    moment = now or dt.datetime.now(dt.UTC)
    content = await load_notification_content(session, delivery["notification_id"])

    if content is None:
        await _cancel(session, delivery, "Notification no longer exists.", now=moment)
        return "cancelled"

    # Re-checked here, not just at claim time: an acknowledgement in the intervening
    # seconds must still stop the send.
    if content["notification_status"] in ("cancelled", "failed"):
        await _cancel(session, delivery, "Notification was cancelled.", now=moment)
        return "cancelled"

    if content["incident_status"] in STOPS_ESCALATION:
        await _cancel(
            session,
            delivery,
            f"Incident was {content['incident_status']} before this was sent.",
            now=moment,
        )
        return "cancelled"

    attachments = await load_attachments(
        session, object_store, incident_id=content["incident_id"]
    )
    media_urls = await load_media_urls(
        session, object_store, incident_id=content["incident_id"]
    )

    # Only email consumes from_name/brand_logo_url/brand_footer_text today (see
    # Message's own docstring) - skip the lookup entirely for every other channel
    # rather than spending a query on fields the provider would just ignore.
    from_name = brand_logo_url = brand_footer_text = None
    if delivery["channel"] == Channel.EMAIL:
        from_name, brand_logo_url, brand_footer_text = await load_branding(
            session, delivery.get("tenant_id")
        )

    return await send_delivery(
        session,
        registry,
        delivery,
        subject=content["subject"],
        body=content["body"],
        attachments=attachments,
        media_urls=media_urls,
        now=moment,
        from_name=from_name,
        brand_logo_url=brand_logo_url,
        brand_footer_text=brand_footer_text,
    )


async def _cancel(
    session: AsyncSession, delivery: dict, reason: str, *, now: dt.datetime
) -> None:
    await session.execute(
        text(
            """
            UPDATE notification_deliveries
            SET status = 'cancelled', next_attempt_at = NULL,
                failure_code = 'cancelled', failure_summary_redacted = :reason,
                updated_at = :now
            WHERE id = :id
            """
        ),
        {"id": delivery["delivery_id"], "reason": reason[:400], "now": now},
    )


async def run_once(
    session_factory,
    registry: ProviderRegistry,
    *,
    object_store=None,
    batch_size: int = 25,
    now: dt.datetime | None = None,
) -> WorkerStats:
    """One pass: recover stalled rows, claim what is due, send each in its own commit."""
    stats = WorkerStats()
    moment = now or dt.datetime.now(dt.UTC)

    async with session_factory() as session, session.begin():
        await _as_platform(session)
        stats.requeued = await requeue_stalled_deliveries(session, now=moment)

    # Claimed and committed before any provider call, so the rows are released promptly
    # and other workers are never blocked behind a slow send.
    async with session_factory() as session, session.begin():
        await _as_platform(session)
        claimed = await claim_due_deliveries(session, limit=batch_size, now=moment)
    stats.claimed = len(claimed)

    for delivery in claimed:
        try:
            async with session_factory() as session, session.begin():
                await _as_platform(session)
                status = await process_delivery(
                    session, registry, delivery, object_store=object_store, now=moment
                )
        except Exception:
            # One poisonous delivery must not stop the queue. The row stays in `sending`
            # and is recovered by requeue_stalled_deliveries on a later pass.
            logger.exception(
                "delivery_failed_unexpectedly",
                extra={"delivery_id": str(delivery["delivery_id"])},
            )
            stats.failed += 1
            continue

        if status == "accepted":
            stats.accepted += 1
        elif status == "cancelled":
            stats.skipped += 1
        else:
            stats.failed += 1

    return stats


async def _as_platform(session: AsyncSession) -> None:
    """Runs the session across all tenants.

    The worker legitimately serves every tenant, and the connection it uses belongs to the
    platform role. `true` scopes the setting to the transaction, so it cannot leak to the
    next checkout of a pooled connection.
    """
    await session.execute(text("SELECT set_config('app.is_platform','true',true)"))


async def run_forever(
    session_factory,
    registry: ProviderRegistry,
    *,
    object_store=None,
    interval_seconds: float = 5.0,
    batch_size: int = 25,
    stop: asyncio.Event | None = None,
) -> None:
    """Polls until stopped.

    Polling rather than listening: the queue is driven by `next_attempt_at` timestamps, so
    a scheduled escalation two hours out has no event to listen for. A five-second poll is
    negligible load against an indexed query and bounds alert latency at five seconds.
    """
    stop = stop or asyncio.Event()
    logger.info("notification_worker_started", extra={"interval": interval_seconds})

    while not stop.is_set():
        started = dt.datetime.now(dt.UTC)
        try:
            stats = await run_once(
                session_factory, registry, object_store=object_store, batch_size=batch_size
            )
            if stats.claimed or stats.requeued:
                logger.info("notification_worker_pass", extra=stats.as_dict())
        except Exception:
            # The loop must outlive any single failure, including the database being
            # briefly unreachable. Alerts are delayed by one interval, not lost.
            logger.exception("notification_worker_pass_failed")

        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=max(0.5, interval_seconds - elapsed)
            )

    logger.info("notification_worker_stopped")


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Stops the loop cleanly on SIGTERM so a container restart finishes the pass it is on."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)
