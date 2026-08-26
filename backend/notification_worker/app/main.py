"""Notification worker entrypoint.

Runs the dispatch loop as its own container. Deliberately not a thread inside the Tenant
API: an API process is scaled, restarted and deployed on the API's schedule, and alert
delivery should not inherit that. Separating them also means a wedged worker cannot take
the API down with it, and the two scale independently - several workers drain the same
queue safely, since claiming uses `FOR UPDATE SKIP LOCKED`.

The database identity is the platform role, because dispatch legitimately spans every
tenant. That role is a member of the `csense_platform` group and so satisfies the RLS
bypass predicate; the worker earns that by never accepting a request from anyone.
"""
from __future__ import annotations

import asyncio

from app.worker import install_signal_handlers, run_forever
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.logging import configure_logging, get_logger
from csense_shared.notifications.bootstrap import build_registry
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

    stop = asyncio.Event()
    install_signal_handlers(stop)

    try:
        await run_forever(
            session_factory,
            registry,
            object_store=object_store,
            interval_seconds=settings.notification_poll_seconds,
            batch_size=settings.notification_batch_size,
            stop=stop,
        )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
