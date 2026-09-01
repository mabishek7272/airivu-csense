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
