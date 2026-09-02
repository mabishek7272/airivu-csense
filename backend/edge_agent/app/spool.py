"""The encrypted offline spool — where events wait out an outage.

When the uplink drops, the pipeline keeps running and every detection lands here instead
of at the API (FLOW-13 step 3). When the link returns, the sync loop drains it oldest
first and deletes each row only once the server has said it has it. That last clause is
the whole design: at-least-once delivery plus the server's unique
`(tenant_id, source_event_id)` (migration 0012) adds up to effectively-once, and it stops
being true the moment anything here deletes a row it has not seen acknowledged.

**SQLite, via the standard library.** No dependency to build for arm64, present on every
target, and — unlike a JSON file or an append-only log we would have to write ourselves —
it already has the two things this needs and they are hard to get right: an atomic commit,
and a crash-recovery story. `sqlite3` is not an approximation of a durable store here, it
is the reason we do not have to build one.

**Durability: WAL, `synchronous=FULL`.** The common advice for WAL is
`synchronous=NORMAL`, and it is wrong for this file. NORMAL does not fsync on commit; it
only syncs at a checkpoint, so a power cut leaves the database *consistent* but silently
short by however many commits were still in the OS page cache. Those commits are events no
other copy of exists anywhere — a Pi losing power mid-outage is not an edge case here, it
is the exact scenario the spool was built for. FULL costs one fsync per append; at the
event rate a single site produces (order events per second, not thousands) that is a price
worth paying, and it is the difference between "we lost the last few minutes" and "we
didn't". WAL itself is still worth having over the rollback journal because a reader (the
sync loop draining) does not block a writer (the source appending), which is the access
pattern this file has.

**Bounded, and it drops the oldest.** A spool must have a ceiling or a long outage fills
the device's disk and takes down more than the spool. When the ceiling is reached the
oldest events are evicted. That is the ring-buffer convention and it keeps the retained
window contiguous and recent — the events most likely to still matter operationally, and
the ones a drain can deliver in an unbroken sequence. **It also means a long outage loses
its earliest events**, which is a real cost, not a technicality: if a site is offline for
two days, the beginning of the incident is what falls off the end. The honest mitigation
is not the eviction policy, it is `dropped_count()` — a persistent, monotonic counter that
travels on every heartbeat (`spool_dropped`) so the server can see the device is losing
events while it is happening. Silent loss is the failure mode; counted loss is a fact
somebody can act on.

**What the file does and does not reveal.** The payload column holds only `seal_row`'s
output — never plaintext, in the main database or in the WAL. What is deliberately *not*
encrypted is the metadata the spool has to sort and size by: a row id, a capture timestamp,
and a byte count. So someone holding the disk learns how many events the device buffered
and when they happened, but not what was seen, on which camera, by which model, or any
imagery. Encrypting the sort key would mean decrypting the entire spool on every drain
just to order it, on the slowest CPU in the system; the metadata leak is the smaller harm
and this is the deliberate trade.

**Concurrency.** Every public method is safe to call from multiple threads: the spool owns
one connection (`check_same_thread=False`) and serialises operations on its own lock. Task
5's sync loop drains while the local detection listener appends, so that is the real
access pattern rather than a hypothetical. Two constraints come with it, and callers must
honour them:

  *One `Spool` object per file, in one process.* The size counters are cached in memory
  and maintained incrementally, so a second process writing the same file would leave both
  of them wrong about it. One agent per device makes this free; do not open the file twice
  to work around something.

  *The methods block.* A commit fsyncs, which on SD-card-class storage can be several
  milliseconds. From async code, call them in a worker thread (`asyncio.to_thread`) rather
  than stalling the event loop.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crypto import MAX_ROW_BYTES, SpoolCryptoError, open_row, seal_row

logger = logging.getLogger(__name__)

# Mirrors `config.AgentSettings`' own defaults so a `Spool` built directly in a test or a
# script behaves like the one the agent runs. Two bounds because either alone lies: rows
# alone let frame-carrying events fill an SD card, bytes alone let a flood of tiny events
# cost unbounded SQLite overhead.
DEFAULT_MAX_ROWS = 50_000
DEFAULT_MAX_BYTES = 2 * 1024**3

# Bumped only by a change that an existing spool file cannot be read under. Nothing reads
# it yet; it is here because the alternative — discovering on a fleet of deployed devices
# that there is no way to tell v1 files from v2 — is not recoverable remotely.
SCHEMA_VERSION = 1

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

# How many rows one eviction pass examines at a time. In steady state a full spool evicts
# exactly one row per append; this only matters when the cap was lowered between runs and
# thousands of rows have to go at once, where it keeps a single transaction bounded.
_EVICTION_CHUNK = 256


class SpoolError(RuntimeError):
    """The spool refused to accept or read something. Always tells the caller which."""


@dataclass(frozen=True)
class SpooledEvent:
    """One event, decrypted, on its way back out.

    `captured_at` is the parsed sort key; the authoritative original is the value inside
    `event`, which is uploaded exactly as the device recorded it (FLOW-13: "edge original
    event identity/timestamps are preserved").
    """

    id: int
    captured_at: dt.datetime
    event: dict[str, Any]


class Spool:
    """The device's local event buffer. See the module docstring for the guarantees."""

    def __init__(
        self,
        path: str | Path,
        key: bytes,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_rows < 1 or max_bytes < 1:
            raise SpoolError("A spool needs a positive row and byte ceiling.")

        self._path = Path(path)
        self._key = key
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        # Re-entrant because eviction runs inside `append`'s own critical section, and a
        # future maintenance helper composing two public methods should not deadlock.
        self._lock = threading.RLock()

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path,
            # The lock above is what serialises access, not sqlite3's own thread check.
            check_same_thread=False,
            # Explicit transactions only - see `_write`. Autocommit mode makes the
            # durability boundary something in this file rather than something the DB-API
            # layer decides for us.
            isolation_level=None,
            timeout=30.0,
        )
        # Set before `_prepare`, which opens a transaction of its own and would otherwise
        # find them missing if its rollback path ever ran.
        self._rows, self._bytes = 0, 0
        self._prepare()
        self._resync()

    # --- Lifecycle ------------------------------------------------------------------------

    def _prepare(self) -> None:
        conn = self._connection
        # Persistent in the file itself; harmless to re-assert on every open.
        conn.execute("PRAGMA journal_mode=WAL")
        # NOT persistent - a per-connection setting, so it has to be set every time or the
        # next process silently runs at SQLite's default. See the module docstring for why
        # FULL and not the usual NORMAL.
        conn.execute("PRAGMA synchronous=FULL")

        with self._write():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spool_events (
                    -- AUTOINCREMENT, not a plain rowid alias. A plain rowid is reused once
                    -- rows are deleted, and this table empties every time the link comes
                    -- back - so ids would restart, and a ciphertext captured from an
                    -- earlier row could be slotted back in under a matching id later. The
                    -- id is the AAD `seal_row` binds to; it must never come round again.
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- Integer microseconds since the epoch, not text: it sorts numerically
                    -- with no collation or timezone-offset ambiguity. The original string
                    -- the device sent is preserved untouched inside the sealed payload.
                    captured_at_us INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                    payload BLOB NOT NULL CHECK (length(payload) > 0)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_spool_events_order "
                "ON spool_events (captured_at_us, id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS spool_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- Writing --------------------------------------------------------------------------

    def append(self, event: Mapping[str, Any], *, captured_at: dt.datetime | None = None) -> int:
        """Seals one event into the spool and returns its row id.

        `captured_at` comes from the event's own field unless one is passed explicitly.
        There is deliberately no fallback to the current clock: substituting a delivery
        time for a capture time is exactly what FLOW-13's conflict policy forbids, and it
        would be invisible afterwards - the event would look genuine while sitting in the
        wrong place in every timeline the operator reads.

        Returns after the row is committed, and (per `synchronous=FULL`) after that commit
        has reached the disk. The caller may treat a return as "this survives a power cut".
        """
        moment = _resolve_captured_at(event, captured_at)
        plaintext = _serialise(event)

        with self._lock:
            # Before anything is written: a row that cannot fit even in an empty spool
            # would evict every event behind it and still not fit. Refusing tells the
            # caller, which is strictly better than a silent cascade of drops.
            if len(plaintext) > self._max_bytes:
                raise SpoolError(
                    f"This event is {len(plaintext)} bytes, larger than the whole spool's "
                    f"{self._max_bytes}-byte budget. Accepting it would evict everything "
                    "already queued and still not fit."
                )
            if len(plaintext) > MAX_ROW_BYTES:
                raise SpoolError(
                    f"This event is {len(plaintext)} bytes; the server would refuse it, so "
                    "it could never be delivered from the spool either."
                )

            with self._write():
                # Two statements because the ciphertext is bound to the row id and the row
                # id does not exist until the insert. Both are in one transaction, so no
                # reader ever sees the placeholder and a crash between them rolls back.
                cursor = self._connection.execute(
                    "INSERT INTO spool_events (captured_at_us, size_bytes, payload) VALUES (?, 1, X'00')",
                    (_to_micros(moment),),
                )
                row_id = int(cursor.lastrowid)
                blob = seal_row(self._key, row_id, plaintext)
                self._connection.execute(
                    "UPDATE spool_events SET payload = ?, size_bytes = ? WHERE id = ?",
                    (blob, len(blob), row_id),
                )
                self._rows += 1
                self._bytes += len(blob)
                self._evict_locked()

            return row_id

    def _evict_locked(self) -> None:
        """Drops oldest-first until both ceilings are met. Runs inside `append`'s
        transaction, so the eviction and the counter that records it commit together - a
        crash cannot leave events dropped without the count that makes it visible.

        Oldest is by the same `(captured_at_us, id)` order a drain uses, so what remains is
        a contiguous recent window rather than a spool with holes in it. Note that a
        late-arriving event whose capture time predates everything queued can therefore
        evict itself: correct by that rule, and better than the alternative of holding a
        row that would jump the queue on the next drain.
        """
        dropped = 0
        while self._rows > self._max_rows or self._bytes > self._max_bytes:
            oldest = self._connection.execute(
                "SELECT id, size_bytes FROM spool_events ORDER BY captured_at_us, id LIMIT ?",
                (_EVICTION_CHUNK,),
            ).fetchall()
            if not oldest:  # pragma: no cover - the counters cannot exceed an empty table
                break

            victims: list[int] = []
            for row_id, size in oldest:
                victims.append(row_id)
                self._rows -= 1
                self._bytes -= size
                if self._rows <= self._max_rows and self._bytes <= self._max_bytes:
                    break

            self._connection.execute(
                f"DELETE FROM spool_events WHERE id IN ({','.join('?' * len(victims))})", victims
            )
            dropped += len(victims)

        if dropped:
            self._bump_dropped(dropped)
            logger.warning(
                "spool_evicted_oldest",
                extra={"dropped": dropped, "depth": self._rows, "bytes": self._bytes},
            )

    def set_limits(self, *, max_rows: int | None = None, max_bytes: int | None = None) -> None:
        """Changes the spool's ceilings while it is running - a `config_push` command's own
        "genuinely observed running, not just on next restart" requirement
        (docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-state.md,
        Task 4). Either argument left `None` leaves that ceiling untouched, so
        `main.py`'s `_run_command` can apply a config-push that only names one of the two
        fields without having to re-supply the other from `RuntimeConfig` itself.

        Guarded by the same `self._lock` `append` and `_evict_locked` already use: `append`
        runs on a thread-pool thread (`asyncio.to_thread`, per this module's own docstring),
        so a limit change racing an in-flight append is a real concurrent-access case, not
        a theoretical one - without the lock, an append could read a half-updated pair of
        ceilings, or this method could read row/byte counts mid-insert.

        A lowered ceiling evicts immediately, inside this same call, rather than waiting for
        the next `append` to notice the spool is over budget - a spool sitting comfortably
        under its *old* limit can otherwise go a long time with no new event to trigger the
        eviction `append` would otherwise perform, during which an operator who just pushed
        a smaller cap would see no effect at all.
        """
        if max_rows is not None and max_rows < 1:
            raise SpoolError("A spool needs a positive row ceiling.")
        if max_bytes is not None and max_bytes < 1:
            raise SpoolError("A spool needs a positive byte ceiling.")

        with self._lock:
            if max_rows is not None:
                self._max_rows = max_rows
            if max_bytes is not None:
                self._max_bytes = max_bytes
            with self._write():
                self._evict_locked()

    # --- Reading --------------------------------------------------------------------------

    def drain(self, limit: int) -> list[SpooledEvent]:
        """The oldest `limit` events, chronologically, **without deleting anything**.

        Deletion happens only in `ack`, once the server has confirmed. A drain whose upload
        dies in flight - a dropped tunnel, a 502, a restarted API - must leave every row
        exactly where it was, or one bad TCP connection costs a batch of evidence that
        exists nowhere else.

        A row that will not decrypt is the single exception: it is deleted and counted as a
        drop. There is nothing to deliver (the plaintext is gone), and leaving it would
        mean it occupies the head of the drain window on every cycle forever.
        """
        if limit < 1:
            raise SpoolError("A drain limit must be at least 1.")

        with self._lock:
            rows = self._connection.execute(
                "SELECT id, captured_at_us, payload FROM spool_events "
                "ORDER BY captured_at_us, id LIMIT ?",
                (limit,),
            ).fetchall()

            events: list[SpooledEvent] = []
            unreadable: list[int] = []
            for row_id, micros, blob in rows:
                try:
                    payload = json.loads(open_row(self._key, row_id, blob))
                except (SpoolCryptoError, ValueError):
                    # Not fatal and not silent: corruption or tampering, logged and counted.
                    logger.error("spool_row_unreadable", extra={"row_id": row_id})
                    unreadable.append(row_id)
                    continue
                events.append(SpooledEvent(id=row_id, captured_at=_from_micros(micros), event=payload))

            if unreadable:
                self._delete(unreadable, count_as_dropped=True)
            return events

    def ack(self, ids: Iterable[int]) -> int:
        """Deletes the rows the server has confirmed it holds. Returns how many went.

        Unknown ids are ignored rather than raising: a device replaying an ack after a
        restart is doing the safe thing, and turning that into an error would stall a drain
        that had actually succeeded.
        """
        wanted = [int(i) for i in ids]
        if not wanted:
            return 0
        with self._lock:
            return self._delete(wanted, count_as_dropped=False)

    def discard(self, ids: Iterable[int]) -> int:
        """Deletes rows that can never be delivered, **counting them as drops**.

        The narrow companion to `ack`, and the distinction between the two is the whole
        reason this exists rather than callers reusing `ack`: `ack` records a success, and
        the events it removes reached the platform. These did not. They are being given up
        on - the server named them permanently rejected, or they outlived the sync engine's
        retry deadline (see `sync.py`'s per-item policy) - and that is loss, indistinguishable
        in its consequences from an eviction. So it goes through the same persistent
        `dropped` counter an eviction does, which is what carries it to the server on the
        next heartbeat as `spool_dropped` and escalates the device to `degraded`.

        A permanently rejected row *has* to be removable, or it sits at the head of every
        drain and the events behind it never leave the device. Counting it is what stops
        that fix from being a silent one.
        """
        wanted = [int(i) for i in ids]
        if not wanted:
            return 0
        with self._lock:
            return self._delete(wanted, count_as_dropped=True)

    def _delete(self, ids: list[int], *, count_as_dropped: bool) -> int:
        with self._write():
            removed = self._connection.execute(
                f"SELECT id, size_bytes FROM spool_events WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            if not removed:
                return 0
            self._connection.execute(
                f"DELETE FROM spool_events WHERE id IN ({','.join('?' * len(removed))})",
                [row[0] for row in removed],
            )
            self._rows -= len(removed)
            self._bytes -= sum(row[1] for row in removed)
            if count_as_dropped:
                self._bump_dropped(len(removed))
            return len(removed)

    # --- Telemetry ------------------------------------------------------------------------

    def depth(self) -> int:
        """Events waiting to be delivered - the heartbeat's `spool_depth`."""
        with self._lock:
            return self._rows

    def size_bytes(self) -> int:
        """Sealed bytes currently held, which is what the disk ceiling is measured against."""
        with self._lock:
            return self._bytes

    def dropped_count(self) -> int:
        """Events evicted or found unreadable over this spool file's whole life - the
        heartbeat's `spool_dropped`.

        Cumulative and never reset, deliberately. The server's degradation rule reads the
        *delta* between heartbeats (`_spool_snapshot` in `tenant_api/app/api/edge.py`); a
        counter that reset on restart would report zero after the reboot that ends most
        outages, which is precisely when the drops happened.
        """
        with self._lock:
            return self._meta_get("dropped")

    def durability_settings(self) -> dict[str, Any]:
        """The pragmas actually in force, read back from the connection.

        Worth logging at start: `synchronous` is per-connection, so "we set it in the
        code" and "this process is running with it" are different claims, and only the
        second one keeps events across a power cut.
        """
        with self._lock:
            return {
                "journal_mode": self._connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": self._connection.execute("PRAGMA synchronous").fetchone()[0],
            }

    # --- Plumbing -------------------------------------------------------------------------

    def _write(self):
        """One transaction, `BEGIN IMMEDIATE`, committed or rolled back explicitly.

        IMMEDIATE takes the write lock up front instead of upgrading mid-transaction, so a
        concurrent writer collides at the start (where the busy timeout retries it) rather
        than at the commit (where SQLite cannot upgrade and raises).

        A rollback re-reads the size counters. They are maintained incrementally inside the
        transaction, so an exception between an increment and the commit would otherwise
        leave this object permanently wrong about how full the file is - which shows up
        later as either a spool that evicts events it did not need to, or one that quietly
        grows past its disk budget.
        """
        return _Transaction(self._connection, on_rollback=self._resync)

    def _resync(self) -> None:
        self._rows, self._bytes = self._measure()

    def _measure(self) -> tuple[int, int]:
        """Reads the size counters once, at open. They are maintained incrementally
        afterwards - see the module docstring's one-process constraint."""
        rows, size = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM spool_events"
        ).fetchone()
        return int(rows), int(size)

    def _meta_get(self, key: str) -> int:
        row = self._connection.execute(
            "SELECT value FROM spool_meta WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _bump_dropped(self, amount: int) -> None:
        """Monotonic increment. Called only from inside an open transaction so the count
        and the deletion it describes commit as one."""
        self._connection.execute(
            "INSERT INTO spool_meta (key, value) VALUES ('dropped', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (amount,),
        )


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, *, on_rollback: Callable[[], None]) -> None:
        self._connection = connection
        self._on_rollback = on_rollback
        self._nested = False

    def __enter__(self) -> sqlite3.Connection:
        if self._connection.in_transaction:
            # `append` opens a transaction and `_evict_locked` writes inside it; SQLite has
            # no nested transactions, so the outermost one owns the commit.
            self._nested = True
        else:
            self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type, *_rest) -> None:
        if self._nested:
            return
        if exc_type is None:
            try:
                self._connection.execute("COMMIT")
                return
            except Exception:
                # A commit can fail on its own - a full disk is the ordinary way - and the
                # caller's in-memory bookkeeping has already been done by this point, so
                # this path needs the same undo as an explicit failure.
                pass
        self._rollback()
        self._on_rollback()

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - the transaction was already unwound
            # Nothing left to undo. Swallowed so it cannot replace the original exception,
            # which is the one that says what actually went wrong.
            pass


def _serialise(event: Mapping[str, Any]) -> bytes:
    """The event as compact JSON. `default=` covers the two types a detection source
    naturally produces that JSON does not - datetimes and UUIDs - because failing to
    serialise here would drop a real event over a formatting detail."""
    try:
        return json.dumps(event, separators=(",", ":"), default=_json_default).encode()
    except (TypeError, ValueError) as exc:
        raise SpoolError(f"This event cannot be stored as JSON: {exc}") from exc


def _json_default(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable")


def _resolve_captured_at(event: Mapping[str, Any], override: dt.datetime | None) -> dt.datetime:
    raw = override if override is not None else event.get("captured_at")
    if raw is None:
        raise SpoolError(
            "This event has no captured_at, and the spool will not invent one: a delivery "
            "time standing in for a capture time is indistinguishable from the real thing "
            "once it is uploaded."
        )
    if isinstance(raw, str):
        try:
            raw = dt.datetime.fromisoformat(raw)
        except ValueError as exc:
            raise SpoolError(f"captured_at '{raw}' is not an ISO-8601 timestamp.") from exc
    if not isinstance(raw, dt.datetime):
        raise SpoolError(f"captured_at must be a timestamp, not {type(raw).__name__}.")
    if raw.tzinfo is None:
        # The same reading `ingest.py` applies to a naive `captured_at`, and for the same
        # reason: it is what a device that omits an offset almost always means, and the two
        # ends of one event must not disagree about which instant it was.
        raw = raw.replace(tzinfo=dt.UTC)
    return raw


def _to_micros(value: dt.datetime) -> int:
    """Exact integer microseconds. Computed from the timedelta rather than via
    `timestamp()`, whose float loses sub-microsecond precision at epoch magnitudes and
    could reorder two events recorded in the same microsecond."""
    delta = value.astimezone(dt.UTC) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _from_micros(micros: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=micros)
