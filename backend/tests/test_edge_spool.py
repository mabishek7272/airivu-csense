"""The edge agent's offline spool: what has to be true for an outage to cost nothing.

The spool is the whole of FLOW-13's step 3 ("stores events in an encrypted local spool").
Every property below is one an outage would otherwise turn into lost evidence, so each is
pinned by a test that fails if the property goes away:

  **Nothing is deleted before the server says it has it.** `drain` hands rows out; only
  `ack` removes them. A drain that deleted as it read would lose an entire batch to one
  dropped TCP connection, and nobody would ever know which events were in it.

  **Order is chronological.** FLOW-13's conflict policy says "edge original event
  identity/timestamps are preserved"; a spool that drained newest-first would deliver a
  night's events backwards and file incidents in the wrong sequence.

  **A crash loses nothing.** The device this runs on is a fanless box on a shelf that gets
  power-cycled by the same breaker as the lights.

  **The file holds no plaintext.** Including in the WAL sidecar, which is where a recently
  written row actually lives before a checkpoint - a test that only looked at the main
  database file would pass while the event sat in the clear next to it.

No database, no network, no `csense_shared` — the agent deliberately does not depend on it
(see `backend/edge_agent/app/__init__.py`).
"""
from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import threading
import uuid

import pytest

# The agent's modules are loaded as a private package rather than imported, because every
# service under backend/ names its package `app` and a bare `import app.spool` would
# resolve to whichever service another test module imported first. Loading it as a package
# (rather than as loose modules, which is all `test_edge_spool_crypto.py` needed) is what
# lets `spool.py` say `from .crypto import ...` the same way it will inside the container.
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
spool_mod = importlib.import_module(f"{_PACKAGE}.spool")
crypto = importlib.import_module(f"{_PACKAGE}.crypto")


# --- Helpers ------------------------------------------------------------------------------

BASE = dt.datetime(2026, 9, 2, 3, 15, tzinfo=dt.UTC)


def event(n: int, *, at: dt.datetime | None = None, extra: str = "") -> dict:
    """A detection shaped like the one the ingest API takes."""
    return {
        "camera_id": f"0f3a2c7e-0000-4000-8000-00000000000{n % 10}",
        "source_event_id": f"cam1-{n:04d}",
        "captured_at": (at or (BASE + dt.timedelta(seconds=n))).isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.91, "bbox": [0.1, 0.1, 0.4, 0.8]}],
        "note": extra,
    }


@pytest.fixture()
def key() -> bytes:
    return crypto.generate_device_key()


@pytest.fixture()
def spool(tmp_path, key):
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as s:
        yield s


def ids_of(records) -> list[str]:
    return [r.event["source_event_id"] for r in records]


# --- Append and drain -----------------------------------------------------------------------

def test_an_appended_event_comes_back_intact(spool):
    spool.append(event(1))

    [record] = spool.drain(10)

    assert record.event == event(1)
    assert spool.depth() == 1


def test_the_original_captured_at_survives_the_round_trip_byte_for_byte(spool):
    """FLOW-13's conflict policy: "edge original event identity/timestamps are preserved".
    The spool must hand back the timestamp the device recorded, not a re-rendered version
    of it and certainly not the delivery time."""
    original = "2026-09-02T03:15:07.123456+05:30"
    spool.append({**event(1), "captured_at": original})

    [record] = spool.drain(10)

    assert record.event["captured_at"] == original


def test_drain_returns_oldest_first_by_capture_time_not_insertion_order(spool):
    """A device that spooled a late-arriving event must still deliver the night in order."""
    spool.append(event(3))
    spool.append(event(1))
    spool.append(event(2))

    assert ids_of(spool.drain(10)) == ["cam1-0001", "cam1-0002", "cam1-0003"]


def test_events_sharing_a_capture_time_keep_their_insertion_order(spool):
    """Two detections in the same frame-time is ordinary. `(captured_at, id)` makes the
    order total, so a drain is reproducible rather than whatever the index felt like."""
    for n in range(5):
        spool.append({**event(n), "captured_at": BASE.isoformat()})

    assert ids_of(spool.drain(10)) == [f"cam1-{n:04d}" for n in range(5)]


def test_drain_respects_its_limit_and_returns_the_oldest_window(spool):
    for n in range(10):
        spool.append(event(n))

    assert ids_of(spool.drain(3)) == ["cam1-0000", "cam1-0001", "cam1-0002"]


def test_an_event_without_a_capture_time_is_refused_rather_than_stamped_with_now(spool):
    """Substituting the delivery time for the capture time is precisely what FLOW-13
    forbids, and it would be invisible afterwards - the event would look genuine."""
    with pytest.raises(spool_mod.SpoolError, match="captured_at"):
        spool.append({"source_event_id": "no-clock", "objects": []})


def test_an_event_that_cannot_be_serialised_is_refused_with_a_reason(spool):
    """A source handing over something JSON cannot hold gets told so, rather than a bare
    TypeError surfacing three frames up inside the listener."""
    with pytest.raises(spool_mod.SpoolError, match="JSON"):
        spool.append({"captured_at": BASE, "objects": {object()}})

    assert spool.depth() == 0


def test_uuids_and_datetimes_inside_an_event_survive_as_strings(spool):
    """The two types a detection source naturally produces that JSON does not. Failing on
    them would drop a real event over a formatting detail."""
    camera = uuid.UUID("0f3a2c7e-0000-4000-8000-000000000001")
    spool.append({"camera_id": camera, "captured_at": BASE})

    [record] = spool.drain(10)
    assert record.event == {"camera_id": str(camera), "captured_at": BASE.isoformat()}


def test_a_datetime_object_is_accepted_as_well_as_a_string(spool):
    spool.append({"source_event_id": "x", "captured_at": BASE})

    [record] = spool.drain(10)
    assert record.captured_at == BASE
    assert record.event["captured_at"] == BASE.isoformat()


def test_a_naive_capture_time_is_read_as_utc(spool):
    """The same reading `ingest.py` applies to a naive `captured_at`, for the same reason:
    it is what a device that omits an offset almost always means, and the two ends of the
    same event must not disagree about it."""
    spool.append({"source_event_id": "x", "captured_at": BASE.replace(tzinfo=None)})

    [record] = spool.drain(10)
    assert record.captured_at == BASE


# --- Nothing is deleted before the server acknowledges it ------------------------------------

def test_draining_deletes_nothing(spool):
    """The property the whole design rests on. At-least-once delivery plus the server's
    unique `(tenant_id, source_event_id)` is only safe if the local copy outlives the
    upload attempt - a drain whose HTTP request dies in flight must leave every row where
    it was."""
    for n in range(3):
        spool.append(event(n))

    first = spool.drain(10)
    second = spool.drain(10)

    assert ids_of(first) == ids_of(second)
    assert spool.depth() == 3


def test_ack_removes_only_the_rows_named(spool):
    for n in range(3):
        spool.append(event(n))
    records = spool.drain(10)

    spool.ack([records[0].id, records[2].id])

    assert ids_of(spool.drain(10)) == ["cam1-0001"]
    assert spool.depth() == 1


def test_a_partial_ack_re_offers_the_remainder(spool):
    """The interrupted-drain case: the server accepted the first half of a batch and the
    connection died. The rest must come back on the next cycle, not vanish."""
    for n in range(6):
        spool.append(event(n))
    delivered = spool.drain(6)[:2]

    spool.ack(r.id for r in delivered)

    assert ids_of(spool.drain(10)) == [f"cam1-{n:04d}" for n in range(2, 6)]


def test_acking_an_unknown_id_is_harmless_and_removes_nothing(spool):
    spool.append(event(1))

    assert spool.ack([9_999_999]) == 0
    assert spool.depth() == 1


def test_acking_nothing_is_a_no_op(spool):
    spool.append(event(1))

    assert spool.ack([]) == 0
    assert spool.depth() == 1


# --- Durability and crash safety -------------------------------------------------------------

def test_the_spool_is_configured_to_survive_power_loss(spool):
    """A unit test cannot pull the plug, so it pins the configuration that decides what
    happens when something else does.

    `synchronous=FULL` is the load-bearing half. The usual advice for WAL is
    `synchronous=NORMAL`, which does not fsync on commit - the database stays *consistent*
    across a power cut but the last commits are simply gone. Those last commits are the
    events nobody else has a copy of, which is the entire reason this file exists.
    """
    settings = spool.durability_settings()

    assert settings["journal_mode"] == "wal"
    assert settings["synchronous"] == 2  # SQLITE_SYNC_FULL


def test_an_event_survives_a_process_that_never_closed_the_spool(tmp_path, key):
    """Power loss, `docker kill`, an OOM kill: no `close()`, no flush, no chance to tidy
    up. Everything committed must still be there for the next process."""
    first = spool_mod.Spool(tmp_path / "spool.sqlite3", key)
    first.append(event(1))
    first.append(event(2))
    del first  # deliberately not closed - the process simply stopped existing

    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as reopened:
        assert ids_of(reopened.drain(10)) == ["cam1-0001", "cam1-0002"]


def test_a_reboot_re_offers_unacked_events_and_never_re_offers_acked_ones(tmp_path, key):
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as first:
        for n in range(4):
            first.append(event(n))
        first.ack([r.id for r in first.drain(2)])

    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as second:
        assert ids_of(second.drain(10)) == ["cam1-0002", "cam1-0003"]


def test_the_dropped_counter_survives_a_restart(tmp_path, key):
    """It has to: a reboot is how most outages end, and a counter that reset on one would
    report zero drops to a server whose whole degradation rule is the delta between
    heartbeats (`_spool_snapshot` in `tenant_api/app/api/edge.py`)."""
    path = tmp_path / "spool.sqlite3"
    with spool_mod.Spool(path, key, max_rows=2) as first:
        for n in range(5):
            first.append(event(n))
        dropped = first.dropped_count()

    assert dropped == 3
    with spool_mod.Spool(path, key, max_rows=2) as second:
        assert second.dropped_count() == 3


# --- Bounded size ----------------------------------------------------------------------------

def test_the_row_cap_evicts_the_oldest_and_counts_what_it_lost(tmp_path, key):
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key, max_rows=3) as s:
        for n in range(6):
            s.append(event(n))

        assert ids_of(s.drain(10)) == ["cam1-0003", "cam1-0004", "cam1-0005"]
        assert s.depth() == 3
        assert s.dropped_count() == 3


def test_the_byte_cap_evicts_too(tmp_path, key):
    """Rows alone would let a spool of frame-carrying events fill an SD card, which is the
    failure that takes the whole device down rather than just the spool."""
    payload = "x" * 4096
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key, max_rows=1000, max_bytes=20_000) as s:
        for n in range(20):
            s.append(event(n, extra=payload))

        assert s.depth() < 20
        assert s.dropped_count() == 20 - s.depth()
        assert s.size_bytes() <= 20_000


def test_an_event_too_large_to_ever_fit_is_refused_at_the_door(tmp_path, key):
    """Accepting it would evict the entire spool to make room for a row that still would
    not fit - one oversized event silently costing every event behind it."""
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key, max_bytes=8192) as s:
        s.append(event(1))

        with pytest.raises(spool_mod.SpoolError, match="larger than the whole spool"):
            s.append(event(2, extra="y" * 20_000))

        assert s.depth() == 1
        assert s.dropped_count() == 0  # refused, not dropped - the caller was told


def test_an_event_the_server_would_refuse_is_never_spooled(tmp_path, key):
    """`crypto.MAX_ROW_BYTES` is derived from the API's own frame ceiling. A row past it
    would fail on every drain forever while consuming eviction budget that belongs to
    events that can actually be delivered, so it is refused where the caller can still
    hear about it."""
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as s:
        with pytest.raises(spool_mod.SpoolError, match="never be delivered"):
            s.append(event(1, extra="z" * (crypto.MAX_ROW_BYTES + 1)))

        assert s.depth() == 0


def test_a_write_that_blows_up_mid_transaction_leaves_the_size_counters_honest(spool, monkeypatch):
    """The ceilings are enforced against counters held in memory, so a rolled-back write
    that left them incremented would make the spool permanently wrong about how full it
    is - evicting events it did not need to, or growing past the disk budget it exists to
    respect. Neither shows up until much later, on a device nobody is looking at."""
    spool.append(event(1))
    before = (spool.depth(), spool.size_bytes())

    def explode(*_args, **_kwargs):
        raise RuntimeError("the disk went away mid-write")

    # Patched at the point *after* the row was written and the counters were bumped but
    # before the commit - the only window where the two can actually disagree. Patching
    # anything earlier would make this test pass whether or not the rollback repairs them.
    monkeypatch.setattr(spool_mod.Spool, "_evict_locked", explode)
    with pytest.raises(RuntimeError):
        spool.append(event(2))

    assert (spool.depth(), spool.size_bytes()) == before
    assert ids_of(spool.drain(10)) == ["cam1-0001"]


def test_depth_and_dropped_start_at_zero(spool):
    assert spool.depth() == 0
    assert spool.dropped_count() == 0


# --- The encryption is real ------------------------------------------------------------------

def test_no_plaintext_reaches_the_disk_in_any_of_sqlites_files(tmp_path, key):
    """The test that makes the word "encrypted" true rather than claimed.

    It searches every file SQLite touches, not just the database: in WAL mode a freshly
    committed row lives in the `-wal` sidecar until a checkpoint moves it, so a test that
    only read `spool.sqlite3` could pass with the event sitting in the clear beside it.
    Deliberately no `close()` before looking - closing checkpoints and would hide exactly
    that.
    """
    needle = "PERSON-AT-LOADING-BAY-ZEBRAQUUX"
    s = spool_mod.Spool(tmp_path / "spool.sqlite3", key)
    s.append(event(1, extra=needle))

    files = sorted(p for p in tmp_path.iterdir() if p.is_file())
    assert [p.name for p in files] != ["spool.sqlite3"], "expected a -wal sidecar to exist too"
    for path in files:
        raw = path.read_bytes()
        assert needle.encode() not in raw, f"plaintext found in {path.name}"
        assert b"source_event_id" not in raw, f"plaintext found in {path.name}"
        assert b"loading" not in raw.lower(), f"plaintext found in {path.name}"
    s.close()


def test_the_stored_blob_is_the_sealed_one_and_opens_only_under_its_own_row_id(tmp_path, key):
    """Belt to the previous test's braces: the column really holds `seal_row`'s output,
    bound to that row, rather than something that merely looks unreadable."""
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key) as s:
        row_id = s.append(event(1))

    conn = sqlite3.connect(tmp_path / "spool.sqlite3")
    blob = conn.execute("SELECT payload FROM spool_events WHERE id = ?", (row_id,)).fetchone()[0]
    conn.close()

    assert json.loads(crypto.open_row(key, row_id, blob))["source_event_id"] == "cam1-0001"
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, row_id + 1, blob)


def test_a_spool_written_under_another_device_key_is_not_readable(tmp_path, key):
    """What a stolen device does not yield: any other device's events."""
    path = tmp_path / "spool.sqlite3"
    with spool_mod.Spool(path, key) as s:
        s.append(event(1))

    with spool_mod.Spool(path, crypto.generate_device_key()) as wrong:
        assert wrong.drain(10) == []


def test_an_unreadable_row_is_dropped_and_counted_rather_than_blocking_the_drain(tmp_path, key):
    """A tampered or corrupt row can never be delivered - there is nothing to deliver. The
    only rows this spool deletes without an acknowledgement are ones that are provably
    undeliverable, and they are counted as drops so the loss reaches the server on the next
    heartbeat instead of being silent.
    """
    path = tmp_path / "spool.sqlite3"
    with spool_mod.Spool(path, key) as s:
        poisoned = s.append(event(1))
        s.append(event(2))

    conn = sqlite3.connect(path)
    conn.execute("UPDATE spool_events SET payload = ? WHERE id = ?", (b"\x00" * 64, poisoned))
    conn.commit()
    conn.close()

    with spool_mod.Spool(path, key) as s:
        assert ids_of(s.drain(10)) == ["cam1-0002"]
        assert s.dropped_count() == 1
        assert s.depth() == 1


# --- Concurrency ------------------------------------------------------------------------------

def test_appending_while_draining_from_another_thread_loses_nothing(tmp_path, key):
    """Task 5 runs a sync loop that drains while the local detection listener appends, so
    this is the real access pattern rather than a hypothetical one. The spool serialises
    its own operations; see its module docstring for the constraint that comes with that."""
    with spool_mod.Spool(tmp_path / "spool.sqlite3", key, max_rows=10_000) as s:
        errors: list[BaseException] = []
        delivered: list[str] = []

        def writer(offset: int):
            try:
                for n in range(100):
                    s.append(event(offset + n))
            except BaseException as exc:  # pragma: no cover - only on a real failure
                errors.append(exc)

        def reader():
            try:
                for _ in range(50):
                    batch = s.drain(10)
                    delivered.extend(ids_of(batch))
                    s.ack(r.id for r in batch)
            except BaseException as exc:  # pragma: no cover - only on a real failure
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(o,)) for o in (0, 1000, 2000)]
        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        remaining = ids_of(s.drain(10_000))
        assert len(set(delivered)) == len(delivered), "an acked event was handed out twice"
        assert set(delivered).isdisjoint(remaining)
        assert len(delivered) + len(remaining) == 300
        assert s.dropped_count() == 0
