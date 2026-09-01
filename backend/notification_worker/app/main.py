"""Notification worker entrypoint.

Runs two independent polling loops as one container: alert dispatch (worker.py, unchanged)
and automatic webhook delivery (webhook_dispatch.py, new). Sharing one container rather
than adding a second is deliberate — CLAUDE.md's own resource math for the production box
(16 cores, no GPU) already treats "reuse infra, don't proliferate containers" as the
default, and both loops already need the exact same platform database role for the exact
same reason ("dispatch legitimately spans every tenant"). One shared `stop` event means a
single SIGTERM cleanly drains both.

The two loops are independent in the direction that matters: webhook delivery failing -
whether its master key will not load or its loop dies outright - leaves alert dispatch
running, while the reverse is not true and is not meant to be (see
`_webhook_dispatch_never_takes_alerts_down_with_it`).

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
            _webhook_dispatch_never_takes_alerts_down_with_it(
                session_factory,
                keyring,
                interval_seconds=settings.webhook_dispatch_poll_seconds,
                batch_size=settings.webhook_dispatch_batch_size,
                max_event_age_seconds=settings.webhook_dispatch_max_event_age_seconds,
                stop=stop,
            )
        )

    try:
        await asyncio.gather(*loops)
    finally:
        await engine.dispose()


async def _webhook_dispatch_never_takes_alerts_down_with_it(
    session_factory,
    keyring,
    *,
    interval_seconds: float,
    batch_size: int,
    max_event_age_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Runs the webhook loop so that its death is never the alert loop's death.

    `asyncio.gather` without `return_exceptions=True` propagates the first exception to the
    caller *without* cancelling its siblings - so an exception escaping the webhook loop
    would unwind `amain`, hit `finally: await engine.dispose()` while alert dispatch is
    still mid-flight against that engine, and crash the process. The container would
    restart, but a bug confined to a developer-facing integration feed would have taken
    every life-safety alert down with it on the way out. That severity inversion is the
    same one the quiet-hours cutoff exists to prevent elsewhere in this codebase, and it is
    the exact opposite of what "two independent polling loops" promises.

    So this loop is deliberately asymmetric with the alert loop: its failure is logged and
    swallowed, leaving alert dispatch running alone (the same end state as a keyring that
    would not load, above). The alert loop is left unguarded on purpose - if *it* dies, the
    container should die and be restarted, because nothing useful remains.

    `run_forever` already catches per-pass exceptions internally, so reaching this handler
    means something outside that guard broke, and the loop does not restart itself here:
    silently respawning a loop that just failed in an unanticipated way would hide it. The
    ERROR log with traceback is the signal.
    """
    try:
        await webhook_dispatch.run_forever(
            session_factory,
            keyring,
            interval_seconds=interval_seconds,
            batch_size=batch_size,
            max_event_age_seconds=max_event_age_seconds,
            stop=stop,
        )
    except asyncio.CancelledError:
        raise  # Shutdown, not failure - must propagate or the process cannot stop.
    except Exception:  # noqa: BLE001 - see this function's own docstring
        logger.exception("webhook_dispatch_loop_died_alert_dispatch_continues")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
