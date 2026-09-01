"""Notification worker entrypoint.

Runs two independent polling loops as one container: alert dispatch (worker.py, unchanged)
and automatic webhook delivery (webhook_dispatch.py, new). Sharing one container rather
than adding a second is deliberate — CLAUDE.md's own resource math for the production box
(16 cores, no GPU) already treats "reuse infra, don't proliferate containers" as the
default, and both loops already need the exact same platform database role for the exact
same reason ("dispatch legitimately spans every tenant"). One shared `stop` event means a
single SIGTERM cleanly drains both.

Both are a container of their own rather than a thread inside the Tenant API: an API
process is scaled, restarted and deployed on the API's schedule, and alert delivery
should not inherit that. Separating them also means a wedged worker cannot take the API
down with it, and the two scale independently - several workers drain the same queue
safely, since claiming uses `FOR UPDATE SKIP LOCKED`.

The database identity is the platform role, because dispatch legitimately spans every
tenant. That role is a member of the `csense_platform` group and so satisfies the RLS
bypass predicate; the worker earns that by never accepting a request from anyone.
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

    try:
        keyring = keyring_from_settings(settings)
    except Exception:  # noqa: BLE001 - webhooks are optional; alerts are not
        # Same "degrade, don't refuse to start" stance as the two blocks above, and here
        # for a sharper reason: the keyring is only the *webhook* loop's dependency, so
        # letting it raise would make a malformed or missing master key stop every
        # life-safety alert this container exists to deliver - a strictly worse outcome
        # than dropping a developer-facing integration feed. Caught rather than assumed
        # loadable because it really does happen: the deployment guide's own
        # `openssl rand -hex 32` writes a file this loader rejects.
        logger.exception("keyring_unavailable_webhook_dispatch_disabled")
        keyring = None

    stop = asyncio.Event()
    install_signal_handlers(stop)

    loops = [
        run_forever(
            session_factory,
            registry,
            object_store=object_store,
            interval_seconds=settings.notification_poll_seconds,
            batch_size=settings.notification_batch_size,
            stop=stop,
        )
    ]
    if keyring is not None:
        loops.append(
            webhook_dispatch.run_forever(
                session_factory,
                keyring,
                interval_seconds=settings.webhook_dispatch_poll_seconds,
                batch_size=settings.webhook_dispatch_batch_size,
                stop=stop,
            )
        )

    try:
        await asyncio.gather(*loops)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
