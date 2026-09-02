"""Task 4 of docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-
state.md: device-level desired-state config push, agent side.

FLOW-13's conflict policy is the checklist this file works against: cloud desired state
wins, expired commands are not executed, and a device applies newer configuration only
after artifact verification. The server-side half (bumping `desired_state_version`,
signing the envelope, the `expires_at` filter every command already goes through) is
covered by `test_edge_command_config_push.py`; this file is entirely the agent's own
response to a `config_push` command once it has one in hand - validation against
`config.py`'s own startup bounds, applying it to the *running* `Spool`/`SyncEngine`/
heartbeat cadence, and reporting `observed_state_version` back.

Loaded by path, the same mechanism every other `backend/edge_agent` test file already uses
(see `test_edge_main_loop_isolation.py`'s own docstring for why): every service under
`backend/` names its package `app`, so a bare `import app.config` would resolve to
whichever service another test module happened to import first.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import importlib.util
import pathlib
import random
import sys
import time

import pytest

_PACKAGE = "csense_edge_agent_config_push_under_test"


def _load_agent_package():
    if _PACKAGE in sys.modules:
        return sys.modules[_PACKAGE]
    root = pathlib.Path(__file__).resolve().parents[1] / "edge_agent" / "app"
    spec = importlib.util.spec_from_file_location(
        _PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_agent_package()
config = importlib.import_module(f"{_PACKAGE}.config")
spool_mod = importlib.import_module(f"{_PACKAGE}.spool")
sync_mod = importlib.import_module(f"{_PACKAGE}.sync")
crypto = importlib.import_module(f"{_PACKAGE}.crypto")
main_mod = importlib.import_module(f"{_PACKAGE}.main")


def _runtime_config(**overrides) -> config.RuntimeConfig:
    defaults = {
        "heartbeat_interval_seconds": 30,
        "spool_max_rows": 50_000,
        "spool_max_bytes": 2 * 1024**3,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 300.0,
    }
    defaults.update(overrides)
    return config.RuntimeConfig(**defaults)


# --- validate_config_push: the agent's own "artifact verification" ----------------------

def test_a_single_field_within_bounds_is_accepted_and_merged():
    current = _runtime_config()
    merged = config.validate_config_push({"heartbeat_interval_seconds": 5}, current=current)

    assert merged.heartbeat_interval_seconds == 5
    # Untouched fields carry over from `current`, not reset to some other default.
    assert merged.spool_max_rows == current.spool_max_rows
    assert merged.backoff_initial_seconds == current.backoff_initial_seconds


def test_multiple_fields_in_one_payload_are_all_applied():
    current = _runtime_config()
    merged = config.validate_config_push(
        {"spool_max_rows": 1000, "spool_max_bytes": 10_000_000}, current=current
    )

    assert merged.spool_max_rows == 1000
    assert merged.spool_max_bytes == 10_000_000


@pytest.mark.parametrize(
    "field,value",
    [
        ("heartbeat_interval_seconds", 0),
        ("heartbeat_interval_seconds", -5),
        ("heartbeat_interval_seconds", 3601),
        ("spool_max_rows", 0),
        ("spool_max_rows", -1),
        ("spool_max_bytes", 0),
        ("backoff_initial_seconds", 0.0),
        ("backoff_initial_seconds", -1.0),
        ("backoff_max_seconds", 0.0),
    ],
)
def test_an_out_of_bounds_value_is_rejected_not_clamped(field, value):
    """The exact bounds `AgentSettings.from_env` enforces at boot - reused, not
    reinvented, per this task's own instruction."""
    with pytest.raises(config.ConfigPushRejected, match=field):
        config.validate_config_push({field: value}, current=_runtime_config())


def test_a_non_numeric_value_is_rejected():
    with pytest.raises(config.ConfigPushRejected, match="must be a number"):
        config.validate_config_push(
            {"heartbeat_interval_seconds": "soon"}, current=_runtime_config()
        )


def test_a_boolean_is_rejected_even_though_python_treats_it_as_an_int():
    """`isinstance(True, int)` is `True` in Python - a payload of `{"heartbeat_interval_
    seconds": true}` must not sneak through as the integer 1."""
    with pytest.raises(config.ConfigPushRejected, match="must be a number"):
        config.validate_config_push(
            {"heartbeat_interval_seconds": True}, current=_runtime_config()
        )


def test_a_payload_naming_no_recognised_field_is_rejected():
    """A config_push that changes nothing this build understands is a bug worth a visible
    rejection, not a silent no-op success."""
    with pytest.raises(config.ConfigPushRejected, match="none of the fields"):
        config.validate_config_push({"model_confidence_threshold": 0.9}, current=_runtime_config())


def test_an_empty_payload_is_rejected():
    with pytest.raises(config.ConfigPushRejected, match="none of the fields"):
        config.validate_config_push({}, current=_runtime_config())


def test_target_version_and_other_envelope_keys_are_ignored_not_rejected():
    """`target_version` travels in the same payload dict (the server's own bookkeeping,
    not an agent-tunable field) - it must not trip the "no recognised field" rejection."""
    merged = config.validate_config_push(
        {"heartbeat_interval_seconds": 10, "target_version": 3}, current=_runtime_config()
    )
    assert merged.heartbeat_interval_seconds == 10


def test_backoff_ceiling_below_floor_is_rejected_using_the_merged_result():
    """The cross-field check has to see the *merged* value, not just the field named in
    this particular payload: lowering only backoff_max_seconds below the device's existing
    backoff_initial_seconds must be caught exactly as if both had arrived together."""
    current = _runtime_config(backoff_initial_seconds=30.0, backoff_max_seconds=300.0)

    with pytest.raises(config.ConfigPushRejected, match="ceiling"):
        config.validate_config_push({"backoff_max_seconds": 5.0}, current=current)


def test_backoff_ceiling_below_floor_is_rejected_when_both_arrive_together():
    with pytest.raises(config.ConfigPushRejected, match="ceiling"):
        config.validate_config_push(
            {"backoff_initial_seconds": 30.0, "backoff_max_seconds": 5.0},
            current=_runtime_config(),
        )


def test_a_valid_backoff_pair_within_bounds_is_accepted():
    merged = config.validate_config_push(
        {"backoff_initial_seconds": 2.0, "backoff_max_seconds": 60.0},
        current=_runtime_config(),
    )
    assert merged.backoff_initial_seconds == 2.0
    assert merged.backoff_max_seconds == 60.0


# --- Spool.set_limits: a lowered ceiling evicts immediately, not on the next append ------

@pytest.fixture()
def spool(tmp_path):
    key = crypto.generate_device_key()
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key, max_rows=1000, max_bytes=10**7) as s:
        yield s


def _event(n: int) -> dict:
    return {
        "camera_id": "0f3a2c7e-0000-4000-8000-000000000001",
        "source_event_id": f"cam1-{n:04d}",
        "captured_at": dt.datetime(2026, 9, 2, tzinfo=dt.UTC).isoformat(),
        "objects": [],
    }


def test_lowering_max_rows_evicts_immediately_without_a_new_append(spool):
    for n in range(10):
        spool.append(_event(n))
    assert spool.depth() == 10

    spool.set_limits(max_rows=3)

    # No new event arrived - this is the "genuinely observed running, not on next
    # restart" requirement: an operator who just pushed a smaller cap must see the spool
    # obey it now, not whenever the next detection happens to come in.
    assert spool.depth() == 3
    assert spool.dropped_count() == 7


def test_raising_max_rows_does_not_evict_anything(spool):
    for n in range(5):
        spool.append(_event(n))

    spool.set_limits(max_rows=2000)

    assert spool.depth() == 5
    assert spool.dropped_count() == 0


def test_set_limits_with_one_argument_leaves_the_other_ceiling_untouched(spool):
    for n in range(10):
        spool.append(_event(n))

    spool.set_limits(max_rows=4)  # max_bytes left alone

    assert spool.depth() == 4
    # A subsequent, generously-sized append is still accepted - proving max_bytes was
    # never touched (it started at 10MB and stays there).
    spool.append(_event(99))
    assert spool.depth() == 4  # the new row evicted the oldest to stay at the row cap


@pytest.mark.parametrize("kwargs", [{"max_rows": 0}, {"max_rows": -1}, {"max_bytes": 0}])
def test_set_limits_refuses_a_non_positive_ceiling(spool, kwargs):
    with pytest.raises(spool_mod.SpoolError):
        spool.set_limits(**kwargs)


# --- SyncEngine.set_backoff: a real behavioural change, not just a stored number --------

class _FakeApi:
    async def send(self, detections):  # pragma: no cover - not exercised in these tests
        raise AssertionError("not used")


def _engine(spool, **kwargs):
    defaults = {
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 10.0,
        "rng": random.Random(0),
    }
    defaults.update(kwargs)
    return sync_mod.SyncEngine(spool=spool, send_fn=_FakeApi().send, **defaults)


def test_set_backoff_changes_the_delay_the_engine_actually_computes(spool):
    """Not an inspection of a private attribute - `next_delay_seconds` is the public
    method the real drain loop calls, so this proves the *behaviour* changed."""
    eng = _engine(spool)
    report = sync_mod.DrainReport(link_failed=True)

    before = eng.next_delay_seconds(report)
    assert 0.5 <= before <= 1.0  # half..full of the 1.0s initial window, no failures yet

    eng.set_backoff(initial_seconds=20.0, max_seconds=40.0)
    after = eng.next_delay_seconds(report)

    assert 10.0 <= after <= 20.0  # half..full of the new 20.0s window


def test_set_backoff_with_one_argument_leaves_the_other_untouched(spool):
    eng = _engine(spool, backoff_initial_seconds=1.0, backoff_max_seconds=10.0)

    eng.set_backoff(initial_seconds=5.0)  # max_seconds left alone

    report = sync_mod.DrainReport(link_failed=True)
    delay = eng.next_delay_seconds(report)
    assert 2.5 <= delay <= 5.0  # still bounded by the untouched 10.0s ceiling, not exceeded


# --- _run_command("config_push", ...): validate, apply, ack -----------------------------

class _AckOnlyClient:
    """Just enough of `httpx.AsyncClient` to observe what `_run_command` acks."""

    def __init__(self) -> None:
        self.acks: list[dict] = []

    async def post(self, url, json):  # noqa: A002 - httpx's own parameter name
        self.acks.append(json)
        return _Response()


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


def _command(*, target_version, payload_extra=None, expires_in_hours=1.0):
    expires_at = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=expires_in_hours)).isoformat()
    return {
        "id": "cmd-1",
        "command_type": main_mod.CONFIG_PUSH_COMMAND_TYPE,
        "expires_at": expires_at,
        "payload": {"target_version": target_version, **(payload_extra or {})},
    }


async def test_a_valid_config_push_is_applied_to_the_running_objects_and_acked_ok(spool):
    eng = _engine(spool)
    runtime_config = _runtime_config()
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001 - the unit under test
        client,
        _command(target_version=7, payload_extra={"heartbeat_interval_seconds": 5, "spool_max_rows": 200}),
        runtime_config=runtime_config, spool=spool, engine=eng,
    )

    assert runtime_config.heartbeat_interval_seconds == 5
    assert runtime_config.spool_max_rows == 200
    # Applied to the *running* Spool, not just recorded on RuntimeConfig.
    assert spool._max_rows == 200  # noqa: SLF001 - proving the real object was mutated
    assert runtime_config.observed_state_version == 7

    [ack] = client.acks
    assert ack["success"] is True
    assert ack["result_code"] == "ok"


async def test_an_out_of_bounds_config_push_is_refused_not_applied(spool):
    eng = _engine(spool)
    runtime_config = _runtime_config(heartbeat_interval_seconds=30)
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        client,
        _command(target_version=9, payload_extra={"heartbeat_interval_seconds": 0}),
        runtime_config=runtime_config, spool=spool, engine=eng,
    )

    # Nothing moved: not the field the payload named, and not the version.
    assert runtime_config.heartbeat_interval_seconds == 30
    assert runtime_config.observed_state_version == 0

    [ack] = client.acks
    assert ack["success"] is False
    assert ack["result_code"] == "out_of_bounds"


async def test_a_config_push_missing_target_version_is_refused(spool):
    eng = _engine(spool)
    runtime_config = _runtime_config()
    client = _AckOnlyClient()

    command = {
        "id": "cmd-2",
        "command_type": main_mod.CONFIG_PUSH_COMMAND_TYPE,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat(),
        "payload": {"heartbeat_interval_seconds": 5},  # no target_version
    }

    await main_mod._run_command(  # noqa: SLF001
        client, command, runtime_config=runtime_config, spool=spool, engine=eng,
    )

    assert runtime_config.observed_state_version == 0
    assert runtime_config.heartbeat_interval_seconds == 30  # never applied
    [ack] = client.acks
    assert ack["success"] is False
    assert ack["result_code"] == "out_of_bounds"


async def test_a_config_push_with_no_runtime_context_wired_is_refused_not_crashed():
    """Only reachable if a future caller wires the command loop without `runtime_config`/
    `spool`/`engine` - `amain()` always supplies all three in production. Still must fail
    safe (a clear ack, never an unhandled exception escaping the command loop `main.py`'s
    own module docstring promises stays isolated from the heartbeat) rather than silently
    doing nothing."""
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        client, _command(target_version=1), runtime_config=None, spool=None, engine=None,
    )

    [ack] = client.acks
    assert ack["success"] is False
    assert ack["result_code"] == "internal_error"


async def test_an_expired_config_push_is_never_applied_observed_version_does_not_move():
    """FLOW-13: expired commands are not executed. The expiry check runs before command-
    type dispatch, so a config_push gets exactly the same treatment `ping` already does -
    this test is the config_push-specific proof the task asks for."""
    runtime_config = _runtime_config()
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        client,
        _command(target_version=3, payload_extra={"heartbeat_interval_seconds": 5}, expires_in_hours=-1.0),
        runtime_config=runtime_config, spool=None, engine=None,
    )

    assert runtime_config.heartbeat_interval_seconds == 30  # untouched default
    assert runtime_config.observed_state_version == 0

    [ack] = client.acks
    assert ack["success"] is False
    assert ack["result_code"] == "expired"


async def test_a_second_valid_push_advances_the_version_again(spool):
    """Desired state converging is not a one-shot: a device that already applied version 7
    must be able to converge on version 8 next."""
    eng = _engine(spool)
    runtime_config = _runtime_config(observed_state_version=7)
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        client,
        _command(target_version=8, payload_extra={"backoff_initial_seconds": 2.0}),
        runtime_config=runtime_config, spool=spool, engine=eng,
    )

    assert runtime_config.observed_state_version == 8
    assert runtime_config.backoff_initial_seconds == 2.0


# --- heartbeat_loop: a runtime_config change takes effect without a restart --------------

class _HeartbeatClient:
    """Answers every heartbeat with the real route's own constant, `next_interval_
    seconds: 30` - unchanged from what a real deployment's `/heartbeat` always returns
    today, which is exactly the case the precedence rule in `heartbeat_loop` has to get
    right (see that function's own comment)."""

    async def post(self, url, json):  # noqa: A002
        return _JsonResponse({"next_interval_seconds": 30})


class _SilentHeartbeatClient:
    """A heartbeat response carrying no `next_interval_seconds` at all - isolates "does
    the loop read `runtime_config` fresh each iteration" from the separate precedence rule
    against the server's own advisory, which `test_after_a_config_push_the_servers_
    constant_response_no_longer_overrides_it` below covers on its own."""

    async def post(self, url, json):  # noqa: A002
        return _JsonResponse({})


class _JsonResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _NoopEngine:
    def snapshot(self):
        return {"spool_bytes": 0, "spool_depth": 0, "spool_dropped": 0, "online": True}

    def note_link_healthy(self):
        return None


async def test_heartbeat_loop_reads_the_interval_fresh_each_iteration_not_once_at_entry():
    """Simulates exactly what a concurrent `config_push` does: mutate `runtime_config.
    heartbeat_interval_seconds` mid-flight, between two loop iterations, with no restart.
    `_wait` is monkeypatched so the test does not actually sleep and so it can observe -
    and drive - what the loop asks it to wait for on each pass."""
    runtime_config = _runtime_config(heartbeat_interval_seconds=30)
    waits: list[float] = []
    stop = asyncio.Event()

    async def fake_wait(_stop, seconds):
        waits.append(seconds)
        if len(waits) == 1:
            # A config_push landing between the first and second heartbeat.
            runtime_config.heartbeat_interval_seconds = 5
        if len(waits) >= 3:
            stop.set()

    original_wait = main_mod._wait
    main_mod._wait = fake_wait
    try:
        await main_mod.heartbeat_loop(
            _SilentHeartbeatClient(), _NoopEngine(), runtime_config=runtime_config, stop=stop
        )
    finally:
        main_mod._wait = original_wait

    assert waits == [30, 5, 5]


async def test_after_a_config_push_the_servers_constant_response_no_longer_overrides_it():
    """The real bug this task's implementation had to avoid: the live `/heartbeat` route
    always returns the same constant `next_interval_seconds`. Once a config_push has
    explicitly named `heartbeat_interval_seconds` (`heartbeat_interval_pinned_by_config_
    push`), that constant must not silently revert the interval on the very next
    heartbeat - which would otherwise discard the pushed value one cycle after acking it
    as applied."""
    runtime_config = _runtime_config(
        heartbeat_interval_seconds=5, heartbeat_interval_pinned_by_config_push=True
    )
    waits: list[float] = []
    stop = asyncio.Event()

    async def fake_wait(_stop, seconds):
        waits.append(seconds)
        if len(waits) >= 2:
            stop.set()

    original_wait = main_mod._wait
    main_mod._wait = fake_wait
    try:
        await main_mod.heartbeat_loop(
            _HeartbeatClient(), _NoopEngine(), runtime_config=runtime_config, stop=stop
        )
    finally:
        main_mod._wait = original_wait

    # Stayed at 5 both times, even though the server answered 30 on every call.
    assert waits == [5, 5]


async def test_a_config_push_that_only_touches_an_unrelated_field_leaves_the_advisory_live(spool):
    """Issue 2 from code review: `observed_state_version` is bumped by *any* applied
    config-push, not only one that names the interval. A push that only changes
    `spool_max_rows` must not permanently silence the server's `next_interval_seconds`
    advisory - the mechanism `edge.py` documents as existing "so the cadence can be
    widened during an incident without shipping firmware". This is the two-signals-
    disagree scenario the original task asked to probe, end to end: apply an unrelated
    push through the real `_run_command`, then prove the advisory still moves the
    interval on the very next heartbeat.
    """
    eng = _engine(spool)
    # Starts away from 30 (`_HeartbeatClient`'s constant answer) specifically so a
    # transition to 30 is observable evidence the advisory fired - if it started at 30
    # already, the guard blocking it and the guard allowing it would look identical.
    runtime_config = _runtime_config(heartbeat_interval_seconds=10)
    command_client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        command_client,
        _command(target_version=1, payload_extra={"spool_max_rows": 999}),
        runtime_config=runtime_config, spool=spool, engine=eng,
    )

    # The push genuinely applied (version moved, spool cap changed) ...
    assert runtime_config.observed_state_version == 1
    assert runtime_config.spool_max_rows == 999
    # ... but never named the interval, so it must not be pinned.
    assert runtime_config.heartbeat_interval_pinned_by_config_push is False
    assert runtime_config.heartbeat_interval_seconds == 10  # untouched by the push itself

    # Now the server widens the cadence for an incident, exactly the advisory's
    # documented purpose - it must still take effect despite the unrelated push.
    waits: list[float] = []
    stop = asyncio.Event()

    async def fake_wait(_stop, seconds):
        waits.append(seconds)
        stop.set()

    original_wait = main_mod._wait
    main_mod._wait = fake_wait
    try:
        await main_mod.heartbeat_loop(
            _HeartbeatClient(), _NoopEngine(), runtime_config=runtime_config, stop=stop
        )
    finally:
        main_mod._wait = original_wait

    # Moved from 10 to the server's 30 within the same iteration that received the
    # response (the update happens before the bottom-of-loop wait call) - the advisory
    # was honoured, not silenced by the earlier, unrelated config-push.
    assert waits == [30]
    assert runtime_config.heartbeat_interval_seconds == 30


async def test_a_config_push_that_touches_the_interval_pins_it(spool):
    """The other half of the same distinction: a push that *does* name the interval must
    set the flag, so a later unrelated advisory cannot silently undo it."""
    eng = _engine(spool)
    runtime_config = _runtime_config(heartbeat_interval_seconds=30)
    client = _AckOnlyClient()

    await main_mod._run_command(  # noqa: SLF001
        client,
        _command(target_version=1, payload_extra={"heartbeat_interval_seconds": 5}),
        runtime_config=runtime_config, spool=spool, engine=eng,
    )

    assert runtime_config.heartbeat_interval_pinned_by_config_push is True
    assert runtime_config.heartbeat_interval_seconds == 5


# --- Issue 1 from code review: spool.set_limits must not block the event loop, and the
# genuine await point that introduces must not expose a partially-applied runtime_config --

class _SlowSpool:
    """A fake standing in for `Spool`: `set_limits` genuinely blocks (`time.sleep`, not
    `asyncio.sleep`) for `delay` seconds, the same shape a real multi-row SQLite DELETE-
    and-fsync sequence has against a spool near capacity on SD-card-class storage - see
    `spool.py`'s own module docstring for why every one of its methods is written to be
    called through `asyncio.to_thread` rather than directly."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.calls = 0

    def set_limits(self, *, max_rows=None, max_bytes=None) -> None:  # noqa: ARG002
        self.calls += 1
        time.sleep(self._delay)


class _NoopBackoffEngine:
    def set_backoff(self, *, initial_seconds=None, max_seconds=None) -> None:  # noqa: ARG002
        return None


async def test_the_event_loop_stays_responsive_while_a_config_push_is_applying():
    """The bug itself: calling `spool.set_limits` directly (not via `asyncio.to_thread`)
    would run `_SlowSpool`'s simulated blocking work as one uninterruptible synchronous
    burst on this coroutine's own step - `asyncio.gather` schedules `_run_command`'s first
    step before the sibling's, and a task with no genuine suspension point anywhere in it
    runs to completion in that single step before the event loop ever gets to the sibling
    at all (not "runs it later" - doesn't start it). So this has to be proven by *when* the
    sibling actually resumes, not merely *whether* it eventually does: with the real
    `asyncio.to_thread` fix, the sibling's own short sleep is timed from near t=0 and
    resumes long before the spool's simulated work finishes; without it, the sibling's
    first step - and its sleep - cannot even begin until the whole blocking call has
    already returned, so it resumes at roughly (spool delay + its own sleep) instead.
    """
    slow_spool = _SlowSpool(delay=0.3)
    runtime_config = _runtime_config()
    client = _AckOnlyClient()
    start = time.monotonic()
    sibling_resumed_at: list[float] = []

    async def sibling():
        await asyncio.sleep(0.03)
        sibling_resumed_at.append(time.monotonic() - start)

    await asyncio.gather(
        main_mod._run_command(  # noqa: SLF001
            client,
            _command(target_version=1, payload_extra={"heartbeat_interval_seconds": 5}),
            runtime_config=runtime_config, spool=slow_spool, engine=_NoopBackoffEngine(),
        ),
        sibling(),
    )

    assert slow_spool.calls == 1
    [elapsed] = sibling_resumed_at
    # Comfortably below the 0.3s spool delay - proves the sibling ran *during* it, not
    # only after it. A blocked event loop would land this close to 0.3s instead.
    assert elapsed < 0.15, f"sibling only resumed after {elapsed:.3f}s - event loop was blocked"


async def test_no_partial_runtime_config_is_visible_while_set_limits_is_still_applying():
    """The race the reviewer asked to be re-confirmed: wrapping `spool.set_limits` in
    `asyncio.to_thread` introduces a real `await` inside `_apply_config_push` where there
    was none before. A coroutine that runs while that await is suspended (here, sampling
    `runtime_config` directly - standing in for `heartbeat_loop` reading the same object)
    must see either the fully-old pair (`heartbeat_interval_seconds`, `observed_state_
    version`) or the fully-new one, never one moved without the other.

    Asserting only that every observed sample is one of the two consistent pairs is not
    enough to prove this test exercises anything real: without the `asyncio.to_thread` fix,
    `_run_command` runs to completion in one synchronous burst before the sampler's very
    first step, so the sampler would only ever observe the *final* state and this weaker
    assertion would pass vacuously. Requiring the pre-push pair to actually appear in
    `samples` is what makes this a genuine concurrency test rather than one that always
    passes regardless of whether the fix is in place.
    """
    slow_spool = _SlowSpool(delay=0.3)
    runtime_config = _runtime_config(heartbeat_interval_seconds=30)
    client = _AckOnlyClient()
    samples: list[tuple[int, int]] = []

    async def sampler():
        for _ in range(8):
            samples.append(
                (runtime_config.heartbeat_interval_seconds, runtime_config.observed_state_version)
            )
            await asyncio.sleep(0.05)  # keeps sampling across the whole 0.3s blocking window

    await asyncio.gather(
        main_mod._run_command(  # noqa: SLF001
            client,
            _command(target_version=5, payload_extra={"heartbeat_interval_seconds": 7}),
            runtime_config=runtime_config, spool=slow_spool, engine=_NoopBackoffEngine(),
        ),
        sampler(),
    )

    # Every sample was one of exactly two consistent pairs - never a mix of the two
    # fields from different moments in time.
    assert set(samples) <= {(30, 0), (7, 5)}
    # The pre-push pair was genuinely observed mid-flight - see this test's own docstring
    # for why this is the assertion that actually distinguishes the fix from the bug.
    assert (30, 0) in samples
    # And the command did in fact apply (proving the test exercised the real transition,
    # not just a sampler that never caught anything mid-flight).
    assert runtime_config.heartbeat_interval_seconds == 7
    assert runtime_config.observed_state_version == 5
