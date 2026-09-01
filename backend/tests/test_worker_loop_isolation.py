"""A dying webhook-dispatch loop must not take alert dispatch down with it.

`notification_worker/app/main.py` runs both loops under one `asyncio.gather`, which by
default propagates the first exception to the caller *without* cancelling its siblings -
so an exception escaping the webhook loop would unwind `amain`, dispose the engine out
from under alert dispatch, and crash the process. A bug confined to a developer-facing
integration feed would have taken every life-safety alert with it.

These tests pin the guard that prevents that, and the deliberate asymmetry around it: the
webhook loop's failure is swallowed and logged, the alert loop's is not. No database
needed - the guard is pure control flow.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest


def _load_main():
    """Loads notification_worker's `app.main` by path.

    Every service names its package `app`, so a plain import resolves to whichever service
    was imported first - the same collision `test_notification_worker.py` documents. Its
    real `app.webhook_dispatch` / `app.worker` imports have to resolve too, so the
    service's own directory goes on `sys.path` for the load and is removed afterwards.
    """
    worker_dir = pathlib.Path(__file__).resolve().parents[1] / "notification_worker"
    saved = {name: mod for name, mod in sys.modules.items() if name == "app" or name.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(worker_dir))
    try:
        path = worker_dir / "app" / "main.py"
        spec = importlib.util.spec_from_file_location("csense_worker_main_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(worker_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.mark.asyncio
async def test_a_dying_webhook_loop_is_swallowed_so_alert_dispatch_survives(monkeypatch):
    main = _load_main()

    async def exploding_run_forever(*args, **kwargs):
        raise RuntimeError("webhook loop hit something unanticipated")

    monkeypatch.setattr(main.webhook_dispatch, "run_forever", exploding_run_forever)

    alert_loop_still_running = False

    async def alert_loop():
        nonlocal alert_loop_still_running
        await asyncio.sleep(0.05)
        alert_loop_still_running = True

    # Exactly how amain() composes them: gather with no return_exceptions.
    await asyncio.gather(
        alert_loop(),
        main._webhook_dispatch_never_takes_alerts_down_with_it(
            object(), object(), interval_seconds=1.0, batch_size=1, stop=asyncio.Event()
        ),
    )

    assert alert_loop_still_running, (
        "the alert loop must run to completion even though the webhook loop raised"
    )


@pytest.mark.asyncio
async def test_cancellation_still_propagates_so_shutdown_works(monkeypatch):
    """Swallowing CancelledError too would make the container unstoppable - SIGTERM would
    never finish draining. The guard re-raises it deliberately."""
    main = _load_main()

    async def cancelled_run_forever(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.webhook_dispatch, "run_forever", cancelled_run_forever)

    with pytest.raises(asyncio.CancelledError):
        await main._webhook_dispatch_never_takes_alerts_down_with_it(
            object(), object(), interval_seconds=1.0, batch_size=1, stop=asyncio.Event()
        )


@pytest.mark.asyncio
async def test_a_healthy_webhook_loop_returns_normally(monkeypatch):
    main = _load_main()
    ran = False

    async def clean_run_forever(*args, **kwargs):
        nonlocal ran
        ran = True

    monkeypatch.setattr(main.webhook_dispatch, "run_forever", clean_run_forever)

    await main._webhook_dispatch_never_takes_alerts_down_with_it(
        object(), object(), interval_seconds=1.0, batch_size=1, stop=asyncio.Event()
    )

    assert ran
