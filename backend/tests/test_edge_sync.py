"""The edge agent's sync engine and detection source: FLOW-13 steps 3-7, in unit form.

The whole of the offline story reduces to a handful of properties, and every one of them
is a way to lose evidence if it stops holding:

  **A row is deleted only after the server has said it holds it.** Everything else in the
  design is downstream of this. A drain that acked on send would turn one dropped TCP
  connection into a batch of events that exist nowhere.

  **A duplicate is a success.** The server dedupes on `(tenant_id, source_event_id)`, so a
  replayed row comes back `accepted: true, duplicate: true`. Treating that as a failure
  would wedge the spool permanently on its own oldest row - which is exactly the shape of
  bug that never shows up in a happy-path test.

  **A permanently rejected row is removed, and counted.** Otherwise the head of the spool
  is a poison pill and nothing behind it ever drains.

  **A transport failure is never the row's fault.** Nothing is acked, nothing is discarded,
  no rejection clock starts.

  **Fresh events do not wait behind a backlog.** A person walking through a restricted door
  right now matters more than an event from yesterday morning.

No network and no database: `send_fn` is injected, the same discipline
`test_webhook_dispatcher.py` uses. `scripts/e2e_edge_spool.py` (Task 7) is what proves the
real HTTP path against the live stack. The one exception is the detection-source tests,
which bind a real socket on 127.0.0.1 - there is no honest way to test an HTTP listener
without one, and it never leaves the loopback interface.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import importlib.util
import json
import pathlib
import random
import sys
import urllib.error
import urllib.request

import pytest

# Same private-package load the spool tests use: every service under backend/ names its
# package `app`, so a bare `import app.sync` resolves to whichever service another test
# module imported first.
_PACKAGE = "csense_edge_agent_pkg_under_test"


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
sync_mod = importlib.import_module(f"{_PACKAGE}.sync")
source_mod = importlib.import_module(f"{_PACKAGE}.source")
main_mod = importlib.import_module(f"{_PACKAGE}.main")
spool_mod = importlib.import_module(f"{_PACKAGE}.spool")
crypto = importlib.import_module(f"{_PACKAGE}.crypto")

BASE = dt.datetime(2026, 9, 2, 3, 15, tzinfo=dt.UTC)


# --- Fixtures and fakes -------------------------------------------------------------------

def event(n: int, *, at: dt.datetime | None = None) -> dict:
    """A detection shaped exactly like the one `POST /ingest/detections/batch` takes."""
    return {
        "camera_id": "0f3a2c7e-0000-4000-8000-000000000001",
        "source_event_id": f"cam1-{n:04d}",
        "captured_at": (at or (BASE + dt.timedelta(seconds=n))).isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.91, "bbox": [0.1, 0.1, 0.4, 0.8]}],
    }


@pytest.fixture()
def spool(tmp_path):
    with spool_mod.Spool(tmp_path / "spool.sqlite3", crypto.generate_device_key()) as s:
        yield s


class FakeApi:
    """The batch endpoint, in about forty lines.

    Records every payload it is handed and answers with the real `BatchIngestOut` shape,
    including the `duplicate` flag living inside `result` where the server actually puts
    it. Behaviour is programmable per `source_event_id` so a single batch can carry the
    mix the real endpoint can return: accepted, duplicate, permanently rejected,
    transiently failed.
    """

    def __init__(self) -> None:
        self.online = True
        self.calls: list[list[dict]] = []
        self.seen: set[str] = set()
        self.errors: dict[str, str] = {}       # source_event_id -> error_code
        # source_event_id -> the server's own `retryable`. Absent means an older server
        # that does not send the field at all, which is the fallback case.
        self.retryable: dict[str, bool] = {}
        self.omit: set[str] = set()            # ids the response says nothing about
        self.reject_batch_containing: set[str] = set()  # batch-level 4xx, as pydantic does
        self.fail_from_call: int | None = None  # transport dies from this call onward
        self.reverse_results = False

    async def send(self, detections: list[dict]) -> dict:
        self.calls.append([dict(d) for d in detections])
        if not self.online:
            raise sync_mod.TransportError("connection refused")
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise sync_mod.TransportError("connection reset mid-drain")

        ids = {d["source_event_id"] for d in detections}
        if ids & self.reject_batch_containing:
            raise sync_mod.BatchRejected(422, "The batch failed validation.")

        results = []
        for item in detections:
            key = item["source_event_id"]
            if key in self.omit:
                continue
            if key in self.errors:
                entry = {
                    "source_event_id": key, "accepted": False,
                    "error_code": self.errors[key], "error_message": "no",
                }
                if key in self.retryable:
                    entry["retryable"] = self.retryable[key]
                results.append(entry)
                continue
            duplicate = key in self.seen
            self.seen.add(key)
            results.append(
                {
                    "source_event_id": key,
                    "accepted": True,
                    "result": {"detection_id": key, "duplicate": duplicate, "rules_evaluated": 0},
                }
            )
        if self.reverse_results:
            results.reverse()
        accepted = sum(1 for r in results if r["accepted"])
        return {"accepted": accepted, "failed": len(results) - accepted, "results": results}


def engine(spool, api, **kwargs):
    defaults = {
        "batch_size": 10,
        "max_batches_per_cycle": 3,
        "idle_interval_seconds": 5.0,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 300.0,
    }
    defaults.update(kwargs)
    return sync_mod.SyncEngine(spool=spool, send_fn=api.send, **defaults)


def sent_ids(api: FakeApi) -> list[list[str]]:
    return [[d["source_event_id"] for d in call] for call in api.calls]


# --- Online: straight through ------------------------------------------------------------

async def test_an_event_is_delivered_immediately_and_never_touches_the_spool(spool):
    api = FakeApi()
    eng = engine(spool, api)

    assert await eng.submit(event(1)) == "delivered"

    assert sent_ids(api) == [["cam1-0001"]]
    assert spool.depth() == 0
    assert spool.dropped_count() == 0


async def test_a_live_event_that_the_server_calls_a_duplicate_is_still_delivered(spool):
    """A retried submission from the source is not an error, and must not be spooled for
    a second attempt - the server already has it."""
    api = FakeApi()
    eng = engine(spool, api)

    await eng.submit(event(1))
    assert await eng.submit(event(1)) == "delivered"

    assert spool.depth() == 0


# --- Offline: spooled, and not lost ------------------------------------------------------

async def test_an_unreachable_api_means_the_event_is_spooled_not_lost(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)

    assert await eng.submit(event(1)) == "spooled"

    assert spool.depth() == 1
    assert spool.drain(10)[0].event == event(1)
    assert spool.dropped_count() == 0


async def test_once_offline_the_agent_stops_dialling_the_api_for_every_event(spool):
    """The first failure is what discovers the outage; the next thousand events must not
    each pay a connection timeout. A device that dialled per event would spend an outage
    burning CPU and holding sockets instead of spooling."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)

    for n in range(1, 6):
        assert await eng.submit(event(n)) == "spooled"

    assert len(api.calls) == 1
    assert spool.depth() == 5


async def test_the_original_captured_at_is_what_gets_uploaded_after_an_outage(spool):
    """FLOW-13's conflict policy: "edge original event identity/timestamps are preserved".
    Not the delivery time, which is what an incident timeline would otherwise show."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(7))

    api.online = True
    await eng.drain_once()

    assert api.calls[-1][0]["captured_at"] == event(7)["captured_at"]


# --- Reconnect and drain -----------------------------------------------------------------

async def test_a_backlog_drains_oldest_first_in_batches(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=10, max_batches_per_cycle=10)
    for n in range(1, 26):
        await eng.submit(event(n))

    api.online = True
    report = await eng.drain_once()

    # The offline probe is call 0; the drain is everything after it.
    drained = sent_ids(api)[1:]
    assert [len(batch) for batch in drained] == [10, 10, 5]
    assert [item for batch in drained for item in batch] == [f"cam1-{n:04d}" for n in range(1, 26)]
    assert report.delivered == 25
    assert spool.depth() == 0


async def test_nothing_is_deleted_before_the_server_confirms_it(spool):
    """The property the entire design rests on. The API takes the batch and then the
    connection dies before an answer arrives - every row must still be in the spool."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.fail_from_call = 2  # the first drain request itself dies in flight
    report = await eng.drain_once()

    assert report.link_failed is True
    assert report.delivered == 0
    assert spool.depth() == 3
    assert spool.dropped_count() == 0


async def test_an_interrupted_drain_re_offers_exactly_the_undelivered_remainder(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=10, max_batches_per_cycle=10)
    for n in range(1, 26):
        await eng.submit(event(n))

    api.online = True
    api.fail_from_call = 3  # first batch lands, second dies in flight
    first = await eng.drain_once()

    assert first.delivered == 10
    assert spool.depth() == 15

    api.fail_from_call = None
    second = await eng.drain_once()

    assert second.delivered == 15
    assert spool.depth() == 0
    # Every event reached the server exactly once as far as the drain is concerned, and
    # the ones that were re-offered are precisely the ones that were never acknowledged.
    redelivered = sent_ids(api)[3:]
    assert [item for batch in redelivered for item in batch] == [
        f"cam1-{n:04d}" for n in range(11, 26)
    ]


async def test_a_row_the_response_says_nothing_about_is_kept_not_acked(spool):
    """A truncated or partial response must never be read as "everything landed"."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.omit = {"cam1-0002"}
    await eng.drain_once()

    assert [r.event["source_event_id"] for r in spool.drain(10)] == ["cam1-0002"]


async def test_rows_are_matched_by_source_event_id_not_by_position(spool):
    """The response's own contract says so, and matching by index would delete the wrong
    spool row if the server ever reordered - the one mistake that silently loses events."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.reverse_results = True
    api.errors = {"cam1-0002": "ingest_failed"}
    await eng.drain_once()

    assert [r.event["source_event_id"] for r in spool.drain(10)] == ["cam1-0002"]


# --- Dedup: a duplicate is a success -----------------------------------------------------

async def test_a_duplicate_is_acked_and_removed_like_any_other_success(spool):
    """The device lost the response to a batch the server had already committed, so the
    replay comes back `duplicate: true`. If that were treated as a failure, the spool
    would retry the same rows forever and never empty - a permanently stuck spool."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.seen = {"cam1-0001", "cam1-0002", "cam1-0003"}  # the server already has all three
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert report.duplicates == 3
    assert report.delivered == 3
    assert spool.dropped_count() == 0  # a duplicate is delivery, never loss


async def test_a_mixed_batch_of_new_and_duplicate_rows_clears_completely(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 5):
        await eng.submit(event(n))

    api.online = True
    api.seen = {"cam1-0002", "cam1-0004"}
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert (report.delivered, report.duplicates) == (4, 2)


# --- Per-item outcomes drive per-item decisions ------------------------------------------

async def test_a_permanently_rejected_row_is_discarded_and_counted_as_a_drop(spool):
    """`not_found` means the server has made a statement about this row that resending the
    identical bytes cannot change. Keeping it would park it at the head of every batch
    forever; dropping it silently would hide a real loss, so it is counted."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.errors = {"cam1-0002": "not_found"}
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert report.discarded == 1
    assert report.delivered == 2
    assert spool.dropped_count() == 1  # visible on the next heartbeat as spool_dropped


async def test_a_transiently_failed_row_is_kept_and_retried_not_discarded(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))

    api.online = True
    api.errors = {"cam1-0002": "ingest_failed"}
    report = await eng.drain_once()

    assert spool.depth() == 1
    assert report.discarded == 0
    assert spool.dropped_count() == 0

    api.errors = {}
    await eng.drain_once()
    assert spool.depth() == 0


async def test_a_clock_skew_rejection_is_retried_because_time_itself_cures_it(spool):
    """`capture_time_in_future` is the one rejection that is not the row's fault forever:
    a device with a fast RTC produces it, and the same bytes are accepted once the wall
    clock catches up or NTP corrects the box. Discarding on the first sight of it would
    throw away real evidence over a flat battery."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(1))

    api.online = True
    api.errors = {"cam1-0001": "capture_time_in_future"}
    await eng.drain_once()

    assert spool.depth() == 1
    assert spool.dropped_count() == 0


# --- The server's own classification wins ------------------------------------------------
#
# `PERMANENT_ERROR_CODES` is a copy, on the device, of a decision the server makes. An edge
# fleet updates in months and the API in days, so the copy is always the stale one: a
# permanent code added server-side would read as retryable on every deployed agent and park
# the row at the head of the spool until its deadline. The server now sends `retryable` per
# item; the local table stays, but only as the answer for a server too old to have sent one.

async def test_the_servers_retryable_flag_overrides_the_local_permanent_table(spool):
    """`not_found` is in this agent's permanent list, but the server said to retry. The
    server is the side that knows - keeping the row is also the safe direction."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(1))

    api.online = True
    api.errors = {"cam1-0001": "not_found"}
    api.retryable = {"cam1-0001": True}
    report = await eng.drain_once()

    assert spool.depth() == 1
    assert report.discarded == 0
    assert spool.dropped_count() == 0


async def test_a_code_this_agent_has_never_heard_of_is_dropped_when_the_server_says_so(spool):
    """The case the flag exists for: a permanent rejection added to the API long after this
    agent shipped. Without the flag it would be retried until its 24h deadline, holding a
    slot in every batch behind it; with it, the device acts on the server's own statement."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(1))
    await eng.submit(event(2))

    api.online = True
    api.errors = {"cam1-0001": "camera_decommissioned"}
    api.retryable = {"cam1-0001": False}
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert report.discarded == 1
    assert report.delivered == 1
    assert spool.dropped_count() == 1  # counted, so the loss reaches the next heartbeat


async def test_an_older_server_that_sends_no_flag_falls_back_to_the_local_table(spool):
    """A device may be talking to an API that predates `retryable`. The table is not dead
    code - it is the answer whenever the field is absent."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(1))
    await eng.submit(event(2))

    api.online = True
    api.errors = {"cam1-0001": "not_found", "cam1-0002": "ingest_failed"}
    api.retryable = {}  # an older server: the field is not in the response at all
    report = await eng.drain_once()

    assert report.discarded == 1   # not_found, per the local table
    assert spool.depth() == 1      # ingest_failed, kept
    assert spool.dropped_count() == 1


async def test_an_entry_with_a_null_source_event_id_acks_nothing(spool):
    """What the server returns when an item was so malformed that its own
    `source_event_id` could not be recovered. It deliberately does not invent one, so there
    is nothing here to match a spool row against - and an unmatched row is kept, never
    deleted. The row still clears eventually, via the rejection deadline.
    """
    async def send(detections):
        return {"accepted": 0, "failed": 1, "results": [
            {"source_event_id": None, "accepted": False, "error_code": "validation_error",
             "retryable": False},
        ]}

    spool.append(event(1))
    eng = sync_mod.SyncEngine(spool=spool, send_fn=send)
    report = await eng.drain_once()

    assert spool.depth() == 1
    assert report.discarded == 0
    assert spool.dropped_count() == 0


def test_a_non_boolean_retryable_is_read_as_absent_not_as_true(spool):
    """A field of the wrong type says nothing, and `bool("false")` is True - the coercion
    that would turn a garbled response into a discarded row."""
    parsed = sync_mod.parse_batch_response(
        {"results": [
            {"source_event_id": "a", "accepted": False, "error_code": "not_found",
             "retryable": "yes"},
        ]}
    )

    assert parsed["a"].retryable is None
    assert parsed["a"].permanently_rejected is True  # falls back to the local table


def test_an_accepted_item_is_never_permanently_rejected_whatever_the_flag_says(spool):
    """`retryable` is only ever consulted for a rejection. An accepted item is acked and
    deleted; nothing about a flag on it may change that."""
    parsed = sync_mod.parse_batch_response(
        {"results": [
            {"source_event_id": "a", "accepted": True, "retryable": False,
             "result": {"duplicate": False}},
        ]}
    )

    assert parsed["a"].permanently_rejected is False


async def test_a_row_that_keeps_failing_is_given_up_on_once_its_deadline_passes(spool):
    """The backstop under the retry policy. A row nothing will ever accept, whose code the
    agent does not recognise as permanent, must not hold a slot in every batch for the
    rest of the device's life."""
    clock = [1000.0]
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, rejection_deadline_seconds=3600.0, now=lambda: clock[0])
    await eng.submit(event(1))
    await eng.submit(event(2))

    api.online = True
    api.errors = {"cam1-0001": "ingest_failed"}
    await eng.drain_once()
    assert spool.depth() == 1  # still held: the deadline has not passed

    clock[0] += 3601.0
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert report.discarded == 1
    assert spool.dropped_count() == 1


async def test_a_transport_failure_never_starts_a_rows_rejection_clock(spool):
    """An outage is not evidence about any row. If a dropped link counted toward the
    give-up deadline, a long enough outage would discard the whole spool on reconnect."""
    clock = [1000.0]
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, rejection_deadline_seconds=60.0, now=lambda: clock[0])
    await eng.submit(event(1))

    for _ in range(5):
        clock[0] += 100.0
        await eng.drain_once()

    assert spool.depth() == 1
    assert spool.dropped_count() == 0

    api.online = True
    await eng.drain_once()
    assert spool.depth() == 0


async def test_a_batch_level_rejection_isolates_the_poison_row_and_keeps_the_rest(spool):
    """FastAPI validates every item before the handler runs, so one malformed row rejects
    the *whole* request with a 422 and no per-item detail. Without isolation that row
    poisons every batch it is drained in, forever."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=8, max_batches_per_cycle=5)
    for n in range(1, 9):
        await eng.submit(event(n))

    api.online = True
    api.reject_batch_containing = {"cam1-0005"}
    report = await eng.drain_once()

    assert spool.depth() == 0
    assert report.discarded == 1
    assert report.delivered == 7
    assert spool.dropped_count() == 1
    assert api.seen == {f"cam1-{n:04d}" for n in range(1, 9)} - {"cam1-0005"}


# --- Backoff -----------------------------------------------------------------------------

async def test_repeated_failure_backs_off_within_a_bounded_jittered_window(spool):
    api = FakeApi()
    api.online = False
    eng = engine(
        spool, api, backoff_initial_seconds=1.0, backoff_max_seconds=64.0,
        rng=random.Random(7),
    )
    await eng.submit(event(1))

    delays = [eng.next_delay_seconds(await eng.drain_once()) for _ in range(12)]

    # Equal jitter, not full jitter: each delay is at least half its exponential window.
    # Full jitter can return a value arbitrarily close to zero, and this floor is exactly
    # what stops a dead uplink becoming a busy loop - `all(d > 0)` would not catch that,
    # since a full-jittered delay is positive too.
    for attempt, delay in enumerate(delays, start=1):
        window = min(64.0, 1.0 * 2 ** (attempt - 1))
        assert window / 2 <= delay <= window
    # Bounded: the ceiling is a real ceiling, not an asymptote.
    assert max(delays) <= 64.0
    # It actually grows, rather than sitting at the initial value.
    assert delays[6] > delays[0]
    # Jittered: a fleet behind one restored uplink must not reconnect in lockstep.
    assert len({round(d, 6) for d in delays[6:]}) > 1


async def test_backoff_resets_once_the_link_comes_back(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, backoff_initial_seconds=1.0, backoff_max_seconds=64.0)
    await eng.submit(event(1))
    for _ in range(6):
        await eng.drain_once()

    api.online = True
    report = await eng.drain_once()

    assert eng.next_delay_seconds(report) == 5.0  # idle_interval_seconds, not a backoff


async def test_a_progressing_drain_does_not_wait_between_batches(spool):
    """Backlog still to go and the last cycle actually removed rows: the next cycle runs
    immediately. Waiting the idle interval per cycle would make a day's backlog take a day
    to clear."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=5, max_batches_per_cycle=2)
    for n in range(1, 21):
        await eng.submit(event(n))

    api.online = True
    report = await eng.drain_once()

    assert spool.depth() == 10
    assert eng.next_delay_seconds(report) == 0.0


async def test_a_cycle_that_removes_nothing_waits_rather_than_spinning(spool):
    """The dangerous case: the server answers, so the link is up, but every row comes back
    transiently failed. Nothing shrinks. Continuing immediately would be a hot loop
    hammering the API."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    await eng.submit(event(1))

    api.online = True
    api.errors = {"cam1-0001": "ingest_failed"}
    report = await eng.drain_once()

    assert spool.depth() == 1
    assert eng.next_delay_seconds(report) == 5.0


async def test_run_forever_stops_on_the_stop_event_and_never_sleeps_zero_while_idle(spool):
    api = FakeApi()
    eng = engine(spool, api)
    stop = asyncio.Event()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 4:
            stop.set()

    await eng.run_forever(stop, sleep=fake_sleep)

    assert slept == [5.0, 5.0, 5.0, 5.0]
    assert api.calls == []  # an empty spool costs no requests at all


async def test_a_long_backoff_does_not_become_a_long_shutdown(spool):
    """The default wait races the stop event. Without that, a device already five minutes
    into its backoff would take five minutes to notice SIGTERM - long past any container
    runtime's patience, so it would be killed mid-cycle rather than stopping cleanly."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, backoff_initial_seconds=600.0, backoff_max_seconds=600.0)
    await eng.submit(event(1))
    stop = asyncio.Event()

    async def stop_shortly():
        await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.timeout(5):
        await asyncio.gather(eng.run_forever(stop), stop_shortly())


async def test_a_dying_send_never_kills_the_loop(spool):
    """`run_forever` is one of several concurrent loops on the device; an unanticipated
    exception in this one must not end the process and take the heartbeat with it."""
    api = FakeApi()
    eng = engine(spool, api)
    await eng.submit(event(1))

    async def exploding(_detections):
        raise RuntimeError("something nobody predicted")

    eng._send_fn = exploding  # noqa: SLF001 - the point is what escapes, not how it got there
    stop = asyncio.Event()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 3:
            stop.set()

    await eng.run_forever(stop, sleep=fake_sleep)

    assert len(slept) == 3
    assert all(s > 0 for s in slept)
    assert spool.depth() == 0  # delivered before the send was swapped out


# --- Backlog must not starve current events ----------------------------------------------

async def test_a_fresh_event_goes_out_immediately_despite_a_large_backlog(spool):
    """The chosen policy, stated: a live event takes the direct path whenever the link is
    up, whatever is queued behind it. A person in a restricted area right now must not
    wait on yesterday's backlog; the server records each event at its own capture time, so
    the backlog still lands in the right place in every timeline (FLOW-13's late-event
    rule)."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=10, max_batches_per_cycle=2)
    for n in range(1, 501):
        await eng.submit(event(n))
    assert spool.depth() == 500

    api.online = True
    eng.note_link_healthy()
    assert await eng.submit(event(9999)) == "delivered"

    assert sent_ids(api)[-1] == ["cam1-9999"]
    assert spool.depth() == 500  # the backlog is untouched, and the fresh event is gone


async def test_one_cycle_drains_a_bounded_slice_of_a_backlog(spool):
    """Bounded per cycle so the drain always yields: to the heartbeat loop, to the command
    loop, and to fresh events. An unbounded drain would hold the uplink for the length of
    the whole backlog."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api, batch_size=10, max_batches_per_cycle=3)
    for n in range(1, 201):
        await eng.submit(event(n))

    api.online = True
    report = await eng.drain_once()

    assert report.batches == 3
    assert report.delivered == 30
    assert spool.depth() == 170


async def test_the_engine_reports_what_the_heartbeat_has_to_send(spool):
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    for n in range(1, 4):
        await eng.submit(event(n))
    api.online = True
    api.errors = {"cam1-0002": "not_found"}
    await eng.drain_once()

    snapshot = eng.snapshot()

    assert snapshot["spool_depth"] == 0
    assert snapshot["spool_dropped"] == 1
    assert snapshot["online"] is True


# --- The detection source ----------------------------------------------------------------

def test_a_detection_missing_captured_at_is_refused_at_the_door():
    with pytest.raises(source_mod.SourceError, match="captured_at"):
        source_mod.normalise_event({k: v for k, v in event(1).items() if k != "captured_at"})


def test_a_detection_without_a_source_event_id_is_refused():
    """It is the key the server dedupes on. Defaulting one would turn a replayed spool row
    into a second incident."""
    with pytest.raises(source_mod.SourceError, match="source_event_id"):
        source_mod.normalise_event({k: v for k, v in event(1).items() if k != "source_event_id"})


@pytest.mark.parametrize(
    "bbox",
    [[0.4, 0.1, 0.1, 0.8], [0.1, 0.1, 0.1, 0.8], [0.1, 0.1, 1.4, 0.8], [0.1, 0.1, 0.4]],
    ids=["inverted", "zero-area", "out-of-range", "wrong-length"],
)
def test_a_box_the_server_would_reject_is_refused_before_it_is_ever_spooled(bbox):
    """Each of these fails the server's `ObjectIn` validator, which runs *before* the
    per-item handler - so one of them in a batch rejects the whole request. Refusing it
    while the producer is still on the line is the only outcome where nothing is lost."""
    bad = event(1)
    bad["objects"][0]["bbox"] = bbox
    with pytest.raises(source_mod.SourceError, match="bbox"):
        source_mod.normalise_event(bad)


def test_fields_the_agent_does_not_forward_are_dropped():
    payload = event(1) | {"edge_device_id": "someone-elses", "junk": "x" * 1000}
    assert "edge_device_id" not in source_mod.normalise_event(payload)
    assert "junk" not in source_mod.normalise_event(payload)


async def test_the_listener_hands_a_posted_detection_to_the_agent(spool):
    api = FakeApi()
    eng = engine(spool, api)
    src = source_mod.LocalHttpSource(eng.submit, host="127.0.0.1", port=0)
    await src.start()
    try:
        body = await asyncio.to_thread(_post, src.port, event(1))
    finally:
        await src.aclose()

    assert body["results"][0]["outcome"] == "delivered"
    assert sent_ids(api) == [["cam1-0001"]]


async def test_the_listener_tells_the_producer_when_an_event_was_only_spooled(spool):
    """A producer that learns its event was buffered rather than delivered can say so in
    its own logs - often the first sign anyone has that a site is offline."""
    api = FakeApi()
    api.online = False
    eng = engine(spool, api)
    src = source_mod.LocalHttpSource(eng.submit, host="127.0.0.1", port=0)
    await src.start()
    try:
        body = await asyncio.to_thread(_post, src.port, event(1))
    finally:
        await src.aclose()

    assert body["results"][0]["outcome"] == "spooled"
    assert spool.depth() == 1


async def test_a_listener_with_a_token_refuses_an_unauthenticated_post(spool):
    api = FakeApi()
    eng = engine(spool, api)
    src = source_mod.LocalHttpSource(eng.submit, host="127.0.0.1", port=0, token="s3cret")
    await src.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            await asyncio.to_thread(_post, src.port, event(1))
        assert caught.value.code == 401

        body = await asyncio.to_thread(_post, src.port, event(1), "s3cret")
        assert body["results"][0]["accepted"] is True
    finally:
        await src.aclose()

    assert spool.depth() == 0


async def test_one_malformed_event_in_a_burst_does_not_reject_the_burst(spool):
    api = FakeApi()
    eng = engine(spool, api)
    src = source_mod.LocalHttpSource(eng.submit, host="127.0.0.1", port=0)
    await src.start()
    bad = event(2)
    del bad["captured_at"]
    try:
        body = await asyncio.to_thread(
            _post, src.port, {"detections": [event(1), bad, event(3)]}
        )
    finally:
        await src.aclose()

    assert [r["accepted"] for r in body["results"]] == [True, False, True]


# --- The command channel (FLOW-13: "expired commands are not executed") ------------------

class FakeHttp:
    """Just enough of `httpx.AsyncClient` for the command path: it records acks."""

    def __init__(self) -> None:
        self.acks: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict):  # noqa: A002 - httpx's own parameter name
        self.acks.append((url, json))
        return _FakeResponse()


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


async def test_an_expired_command_is_never_executed_only_acked():
    """The server filters expired commands out of `/pending` at the moment it answers, but
    a command can expire in the gap between being handed over and being reached - during a
    long drain, or across a restart. An expiry window enforced at one end only is not
    enforced."""
    http = FakeHttp()
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()

    await main_mod._run_command(  # noqa: SLF001 - the unit under test
        http, {"id": "c1", "command_type": "ping", "expires_at": past}
    )

    [(url, body)] = http.acks
    assert url == "/api/v1/tenant/edge/commands/c1/ack"
    assert body["success"] is False
    assert body["result_code"] == "expired"


async def test_a_live_command_this_build_implements_is_executed_and_acked():
    http = FakeHttp()
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()

    await main_mod._run_command(  # noqa: SLF001
        http, {"id": "c2", "command_type": "ping", "expires_at": future}
    )

    assert http.acks[0][1]["success"] is True


async def test_a_command_this_build_cannot_run_is_refused_rather_than_ignored():
    """An unacked command sits in `delivered` on the operator's console forever, which
    reads as "the device is working on it" and is a lie."""
    http = FakeHttp()
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()

    await main_mod._run_command(  # noqa: SLF001
        http, {"id": "c3", "command_type": "reboot", "expires_at": future}
    )

    assert http.acks[0][1]["result_code"] == "unsupported_command"


def test_a_bad_credential_or_a_rate_limit_is_a_transport_failure_not_a_poison_row():
    """401 and 429 must not send the engine bisecting: neither says anything about the
    rows, and treating one as poison would discard a whole spool over an expired token."""
    for status in (401, 403, 429, 500, 502):
        with pytest.raises(sync_mod.TransportError):
            sync_mod.raise_for_batch_response(status, "no")


def test_a_body_level_refusal_is_what_triggers_isolation():
    for status in (400, 413, 422):
        with pytest.raises(sync_mod.BatchRejected):
            sync_mod.raise_for_batch_response(status, "no")
    assert sync_mod.raise_for_batch_response(202, "") is None


def _post(port: int, payload: dict, token: str | None = None) -> dict:
    request = urllib.request.Request(  # noqa: S310 - a literal loopback URL
        f"http://127.0.0.1:{port}/detections",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read())
