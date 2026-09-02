"""A dying guarded loop must not take the heartbeat - or the process - down with it.

`main.py`'s own module docstring states the asymmetry: the heartbeat loop is deliberately
left unguarded (a device nobody can see is worse than a device that has stopped, so its
death should end the process and let the container restart it), while the sync loop and
the command loop are each wrapped in `_isolated`. `amain` composes all of them under one
`asyncio.gather`, which - by default, with no `return_exceptions=True` - propagates the
first exception to the caller *without* cancelling its siblings. That is exactly the
behaviour wanted for the heartbeat and exactly the behaviour that must not happen for the
loops wrapped in `_isolated`; whether it actually holds is a fact about `_isolated`, not
about `gather`, and untested code is the one place claims like that go stale silently.

This is the identical pattern `test_worker_loop_isolation.py` already pins for
`notification_worker/app/main.py`'s own `_webhook_dispatch_never_takes_alerts_down_with_it`
- `main.py`'s docstring cites that file by name as the shape this was modelled on. These
tests mirror its three cases for the edge agent's own guard: a dying guarded loop is
swallowed while its sibling keeps running, `CancelledError` still propagates so shutdown
actually works, and a healthy loop returns cleanly. No network and no spool - `_isolated`
is pure control flow, so nothing else needs to be real for these to mean something.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pathlib
import sys

import pytest

_PACKAGE = "csense_edge_agent_main_under_test"


def _load_agent_main():
    """Loads edge_agent's `app.main` by path.

    Same reasoning, and the same mechanism, as `test_edge_sync.py`'s own loader: every
    service under `backend/` names its package `app`, so a bare `import app.main` would
    resolve to whichever service another test module happened to import first. `main.py`
    imports `.config`, `.crypto`, `.source`, `.spool` and `.sync` with relative imports, so
    it is the whole package - registered with `submodule_search_locations`, not just this
    one file - that has to be loaded for those to resolve.

    Registered under a name private to this module (distinct from `test_edge_sync.py`'s
    own `csense_edge_agent_pkg_under_test`) so the two test modules loading the same
    on-disk package independently cannot collide in `sys.modules` if collected together.
    """
    if _PACKAGE in sys.modules:
        return sys.modules[f"{_PACKAGE}.main"]
    root = pathlib.Path(__file__).resolve().parents[1] / "edge_agent" / "app"
    spec = importlib.util.spec_from_file_location(
        _PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{_PACKAGE}.main")


main_mod = _load_agent_main()


async def test_a_dying_guarded_loop_is_swallowed_so_its_sibling_keeps_running():
    sibling_finished = False

    async def sibling():
        nonlocal sibling_finished
        await asyncio.sleep(0.05)
        sibling_finished = True

    async def exploding():
        raise RuntimeError("sync loop hit something unanticipated")

    # Exactly how amain() composes the guarded loops under the heartbeat: gather with no
    # return_exceptions, one guarded loop that dies, one healthy sibling.
    await asyncio.gather(
        sibling(),
        main_mod._isolated("sync_loop", exploding()),  # noqa: SLF001
    )

    assert sibling_finished, (
        "a sibling loop must run to completion even though a guarded loop raised"
    )


async def test_cancellation_still_propagates_so_shutdown_works():
    """Swallowing `CancelledError` too would make the container unstoppable - SIGTERM would
    set `stop` and then wait forever for a loop that silently ate its own cancellation. The
    guard re-raises it deliberately; this is the one exception `_isolated` must not catch."""

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await main_mod._isolated("sync_loop", cancelled())  # noqa: SLF001


async def test_a_healthy_guarded_loop_returns_cleanly():
    ran = False

    async def clean():
        nonlocal ran
        ran = True

    await main_mod._isolated("sync_loop", clean())  # noqa: SLF001

    assert ran
