"""Delivering events — live when the link is up, from the spool when it comes back.

This is FLOW-13 steps 3-7 in one object. `submit` is the live path; `drain_once` is the
resync path; `run_forever` is the loop that ties them together with a bounded, jittered
backoff. Everything here exists to preserve one property:

    **A spooled row is deleted only after the server has said it holds it.**

At-least-once delivery from this side, plus the server's unique
`(tenant_id, source_event_id)` (migration 0012), is what adds up to effectively-once. The
moment anything in this file deletes a row it has not seen acknowledged, the platform
starts losing evidence in a way nobody can detect afterwards.

---

**The response is per item, so the decision is per item.** `POST /ingest/detections/batch`
returns one entry per submitted detection, keyed by `source_event_id` (never by position -
matching by index would delete the wrong row if the server ever reordered). Four outcomes,
four different things to do, and getting any of them wrong wedges or drains the spool:

  *`accepted: true`* → ack and delete. **Including when `result.duplicate` is true.** A
  duplicate is the design working: the device lost a response to a batch the server had
  already committed and is replaying it. Treating it as a failure would leave the same rows
  at the head of the spool being re-sent forever — a permanently stuck spool that looks,
  from the outside, exactly like a busy one.

  *A permanently rejected item* (`retryable: false`, or `PERMANENT_ERROR_CODES` when the
  server did not say) → delete it, **count it as a drop**, and log it with its
  `source_event_id`. The server has made a statement about this specific row that re-sending
  the identical bytes cannot change: the camera does not exist in this tenant, or the
  payload is malformed. Keeping it would park it at the head of every drain — `batch_size`
  of these in a row and nothing behind them ever leaves the device. Counting the discard is
  what stops the loss being silent: it reaches the server on the next heartbeat as
  `spool_dropped`, and the platform escalates the device to `degraded`.

  *Any other rejection* → keep it and retry, up to a deadline. `ingest_failed` is the
  server saying "something broke, try again". `capture_time_in_future` is deliberately in
  this group and not the permanent one: it is the one rejection that time itself cures — a
  device with a fast RTC produces it, and the same bytes are accepted once the clock is
  corrected or the wall clock catches up. Discarding real evidence over a flat battery is
  not a trade worth making. The deadline (`rejection_deadline_seconds`, 24h by default) is
  the backstop under all of it: a row nothing will ever accept, carrying a code this agent
  does not recognise as permanent, still cannot hold a slot in every batch for the life of
  the device.

**Which of those two a rejection is, the server decides.** Each entry carries `retryable`,
and it wins over this file's own `PERMANENT_ERROR_CODES` whenever it is present. The list
here is a copy of a decision the API makes, and an edge fleet updates in months while the
API updates in days, so the copy is always the stale one — a permanent code added
server-side would read as retryable on every deployed agent and hold the row until its
deadline. The local table is kept strictly as the fallback for an API too old to send the
field, since an agent in the field outlives the server it shipped against.

  *No entry at all for a row* → keep it. A truncated or partial response is not an
  acknowledgement of anything it does not mention. An entry whose `source_event_id` is null
  counts as no entry: the server returns one when an item was too malformed to recover the
  id from, and refusing to invent one is right — an id that matches nothing deletes
  nothing, while a guessed one could delete the wrong row.

**A transport failure is never the row's fault.** A refused connection, a timeout, a 5xx, a
401: nothing is acked, nothing is discarded, and no row's rejection clock starts. Only the
backoff advances. Were it otherwise, a long enough outage would discard the spool on
reconnect, which inverts the entire point of having one.

**A batch-level 4xx is still isolated by bisection**, though it should now be rare. The
current API validates each item inside its handler, so a malformed row comes back as its
own `validation_error` entry rather than 422-ing the whole request. That was not always
true — an API that types its batch items rejects the entire body with no per-item detail —
and it is not the only way to get here: an oversized body, a proxy's own 400, an envelope
this agent got wrong. On a `BatchRejected` the engine splits the batch and re-sends each
half, converging on the offending row in about log2(n) requests, then discards that single
row and delivers the rest. Re-sending the halves is safe for the same reason every replay
here is safe: the server dedupes. `source.py` refuses the likely offenders at the door too.
The path stays because "should be rare" is not "cannot happen", and the alternative is a
spool that never drains again.

---

**Backlog versus fresh events: the live path always wins.** When the link is believed up, a
freshly submitted event is delivered directly, whatever is queued behind it, and is spooled
only if that delivery fails. The backlog drains separately, at most
`max_batches_per_cycle` batches per cycle. The reasoning: a person walking through a
restricted door *right now* is what the operator needs to see, and making them wait behind
yesterday's backlog would mean the longer an outage was, the later every subsequent alert
arrives. The cost is that a fresh event can reach the server ahead of older ones from the
same camera, so the older ones arrive as late events and may correlate into their own
incident — which is explicitly within FLOW-13's conflict policy ("late events may create
incidents but notification behavior follows tenant late-event rules"), and is why the
server records original capture time rather than arrival time. The drain being *bounded*
per cycle is the other half: it yields between cycles, so it can never hold the uplink or
the event loop for the length of a whole backlog.

**Backoff: exponential from 1s, ceiling 300s, equal jitter.** The ceiling matches
`OFFLINE_AFTER` in `tenant_api/app/api/edge.py` — the five minutes after which the platform
declares a device offline. That is the right ceiling because it makes the agent's worst-case
reconnect delay the same order as the server's own patience: a link restored at minute six
of an outage is discovered within about five minutes, not twenty, while the spool is still
filling. Longer would let a recovered site sit idle with a growing spool for no reason;
much shorter would mean a site's whole fleet hammering the API the instant a shared uplink
returns — which is precisely when it is least able to absorb it. Jitter is *equal* jitter
(half the window fixed, half random) rather than full jitter, because full jitter can return
a delay near zero and this loop's floor is what stops a dead uplink becoming a busy loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from .spool import Spool, SpooledEvent

logger = logging.getLogger(__name__)

# The **fallback** classification, used only when the server's own `retryable` flag is
# absent from an entry - an API older than that field, or a proxy that reshaped the body.
# It is not the primary answer any more, and deliberately not deleted either: an agent in
# the field long outlives the deployment it was shipped against, and having no opinion at
# all would mean retrying a genuinely permanent rejection until its 24-hour deadline.
#
# Deliberately short: anything not listed is retried, so a code this agent has never heard
# of errs toward keeping the event. See this module's docstring for why
# `capture_time_in_future` is absent.
PERMANENT_ERROR_CODES = frozenset(
    {
        # No such camera in this tenant: a misconfigured device, or a camera that has been
        # deleted. Either way these rows have nowhere to land.
        "not_found",
        # The server refused the shape of the payload itself.
        "validation_error",
        "payload_too_large",
    }
)

# HTTP statuses that mean "this request's body is wrong", as opposed to "try again later".
# Used by `raise_for_batch_response` to decide which exception a real client raises, and
# therefore whether the engine isolates a poison row or simply backs off. 401/403/429 and
# every 5xx are deliberately absent: a bad credential or a rate limit says nothing about
# the rows, and treating it as poison would discard a spool over an expired token.
ISOLATING_STATUSES = frozenset({400, 413, 422})

# A cap on how many rows the engine tracks rejection clocks for. Only rows the server has
# explicitly rejected per item get an entry, so this should never be approached; if it is,
# the whole table is dropped rather than grown without bound. Losing the clocks restarts
# the deadlines, which errs toward keeping events - the safe direction.
_MAX_TRACKED_REJECTIONS = 10_000


class TransportError(RuntimeError):
    """The API could not be reached, or answered in a way that says nothing about the rows.

    A refused connection, a timeout, a TLS failure, a 5xx, a 401, a 429. The batch is
    untouched: nothing was acked, nothing was rejected, and the engine backs off.
    """


class BatchRejected(RuntimeError):
    """The server refused the whole request on the body's own terms (400/413/422).

    Distinct from `TransportError` because it *is* evidence about the rows - one of them is
    malformed - and the engine responds by bisecting to find which.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message


# What the engine is handed to talk to the platform. One call, one batch, returning the
# decoded `BatchIngestOut` body. Injected rather than constructed here so unit tests make
# no network call - the same discipline `csense_shared.webhooks.dispatcher` uses with its
# own `send_fn` - and so the HTTP details live in one place (`main.py`) rather than being
# threaded through the delivery logic.
SendFn = Callable[[list[dict[str, Any]]], Awaitable[Mapping[str, Any]]]


def raise_for_batch_response(status_code: int, message: str = "") -> None:
    """Maps an HTTP status onto the two exceptions the engine understands.

    Lives here, next to the policy that consumes it, so the classification is a single
    testable fact rather than a condition buried in a client.
    """
    if 200 <= status_code < 300:
        return
    if status_code in ISOLATING_STATUSES:
        raise BatchRejected(status_code, message or "The batch was rejected.")
    raise TransportError(f"The API answered {status_code}: {message}")


@dataclass(frozen=True)
class ItemOutcome:
    """One entry of the server's per-item response, in the shape this file reasons about."""

    source_event_id: str
    accepted: bool
    duplicate: bool
    error_code: str | None = None
    error_message: str | None = None
    # The server's own verdict on whether re-sending could ever work. `None` means it did
    # not say - an older API, or a body that did not carry the field - not "unknown, assume
    # retryable"; the two are settled differently below.
    retryable: bool | None = None

    @property
    def permanently_rejected(self) -> bool:
        """Whether this row will never be accepted, so the device should drop and count it.

        The server's flag wins whenever it is present. It has to: this agent's own
        `PERMANENT_ERROR_CODES` is a copy of a decision the API makes, and an edge fleet
        updates in months while the API updates in days, so the copy is always the stale
        one. A permanent code added server-side would read as retryable here and park the
        row at the head of every batch until its deadline expired - and, in the other
        direction, a code this agent lists that the server has since made transient would
        be discarded as real, recoverable evidence.

        The local table remains the answer when the field is absent, which is the only
        thing an agent talking to an older API has to go on.
        """
        if self.accepted:
            return False
        if self.retryable is not None:
            return not self.retryable
        return self.error_code in PERMANENT_ERROR_CODES


@dataclass
class DrainReport:
    """What one drain cycle actually did. Drives the next delay, and the logs."""

    batches: int = 0
    delivered: int = 0      # acked and deleted, duplicates included
    duplicates: int = 0     # of which the server already had
    discarded: int = 0      # deleted without ever being delivered - real, counted loss
    retained: int = 0       # left in the spool for another attempt
    link_failed: bool = False

    @property
    def progressed(self) -> bool:
        """Whether the spool actually got shorter. The distinction that keeps a cycle
        returning nothing but transient rejections from becoming a hot loop."""
        return (self.delivered + self.discarded) > 0


def parse_batch_response(body: Mapping[str, Any]) -> dict[str, ItemOutcome]:
    """The server's `BatchIngestOut`, keyed by `source_event_id`.

    `duplicate` lives inside `result`, which is present only for accepted items - reading
    it off the top level would silently see `False` for every duplicate, and the ack path
    would still be right by accident while a future reader of `duplicate` was not.

    An entry whose `source_event_id` is null is skipped, and that is a real case rather
    than defensive padding: the server returns one for an item so malformed that it could
    not recover the id, deliberately refusing to invent one. There is nothing here to match
    a spool row against, so it authorises nothing; the row is kept and cleared by its
    rejection deadline instead.
    """
    outcomes: dict[str, ItemOutcome] = {}
    for entry in body.get("results") or []:
        if not isinstance(entry, Mapping):
            continue
        key = entry.get("source_event_id")
        if not isinstance(key, str):
            # Nothing can be matched to this, so it cannot authorise deleting anything.
            logger.warning("batch_result_without_source_event_id")
            continue
        result = entry.get("result") or {}
        retryable = entry.get("retryable")
        outcomes[key] = ItemOutcome(
            source_event_id=key,
            accepted=bool(entry.get("accepted")),
            duplicate=bool(result.get("duplicate")) if isinstance(result, Mapping) else False,
            error_code=entry.get("error_code"),
            error_message=entry.get("error_message"),
            # Only a real boolean counts as the server having spoken. Anything else - a
            # string, a number, a field a proxy mangled - falls back to the local table,
            # because `bool("false")` is True and that coercion would turn a garbled
            # response into a deleted row.
            retryable=retryable if isinstance(retryable, bool) else None,
        )
    return outcomes


@dataclass
class _Rejections:
    """When each row was first rejected by the server, for the give-up deadline."""

    first_seen: dict[int, float] = field(default_factory=dict)

    def note(self, row_id: int, now: float) -> float:
        if len(self.first_seen) >= _MAX_TRACKED_REJECTIONS:
            logger.warning("rejection_clock_table_reset", extra={"tracked": len(self.first_seen)})
            self.first_seen.clear()
        return self.first_seen.setdefault(row_id, now)

    def forget(self, row_ids: Sequence[int]) -> None:
        for row_id in row_ids:
            self.first_seen.pop(row_id, None)


class SyncEngine:
    """Live delivery, spool drain, and the backoff between attempts.

    The spool's methods block on an fsync, so every call to them here goes through
    `asyncio.to_thread` - stalling the event loop on SD-card-class storage would stall the
    heartbeat and the command channel with it. That constraint is stated in `spool.py`'s
    own docstring; this is the caller that has to honour it.
    """

    def __init__(
        self,
        *,
        spool: Spool,
        send_fn: SendFn,
        batch_size: int = 100,
        max_batches_per_cycle: int = 5,
        idle_interval_seconds: float = 5.0,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 300.0,
        rejection_deadline_seconds: float = 24 * 3600.0,
        now: Callable[[], float] = monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._spool = spool
        self._send_fn = send_fn
        self._batch_size = max(1, batch_size)
        self._max_batches = max(1, max_batches_per_cycle)
        self._idle_interval = idle_interval_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._rejection_deadline = rejection_deadline_seconds
        self._now = now
        self._rng = rng or random.Random()

        self._online = True
        self._consecutive_failures = 0
        self._rejections = _Rejections()

    # --- State the rest of the agent asks about -------------------------------------

    @property
    def online(self) -> bool:
        """The agent's *belief* about the link, not a measurement. It is set false by a
        failed send and true by a successful one, and it exists so an outage costs one
        connection attempt rather than one per event."""
        return self._online

    def note_link_healthy(self) -> None:
        """Told by another loop - the heartbeat - that the platform just answered.

        Without this, a device whose spool is empty when the link returns has nothing to
        probe with and stays in `offline` belief until its next event, which would then
        pay a needless spool round trip. The heartbeat is already talking to the same API
        every 30 seconds; sharing that fact is free.
        """
        if not self._online:
            logger.info("uplink_recovered_per_heartbeat")
        self._online = True
        self._consecutive_failures = 0

    def snapshot(self) -> dict[str, Any]:
        """The numbers the heartbeat reports (`spool_depth` / `spool_dropped`)."""
        return {
            "spool_depth": self._spool.depth(),
            "spool_dropped": self._spool.dropped_count(),
            # Not part of the heartbeat's contract, but the number that says whether a
            # deep spool is about to start evicting - depth alone cannot, since one
            # frame-carrying event costs what a thousand bare ones do.
            "spool_bytes": self._spool.size_bytes(),
            "online": self._online,
            "consecutive_failures": self._consecutive_failures,
        }

    # --- The live path ---------------------------------------------------------------

    async def submit(self, event: Mapping[str, Any]) -> str:
        """Delivers one freshly observed event, or spools it. Returns what happened.

        Tried directly whenever the link is believed up, regardless of how deep the spool
        is - see the module docstring for why fresh events are not made to queue behind a
        backlog, and what that costs.
        """
        if not self._online:
            # No dial attempt: during an outage this path runs once per event, and paying
            # a connect timeout each time would spend the outage blocking the producer
            # instead of buffering for it.
            await self._append(event)
            return "spooled"

        try:
            body = await self._send_fn([dict(event)])
        except BatchRejected as exc:
            # A single event the server refuses on the body's own terms. There is no
            # smaller batch to bisect into, and spooling it would only rediscover this.
            logger.warning(
                "live_event_rejected",
                extra={"source_event_id": event.get("source_event_id"), "detail": exc.message},
            )
            return "rejected"
        except TransportError:
            self._go_offline()
            await self._append(event)
            return "spooled"
        except Exception:  # noqa: BLE001 - an unanticipated client bug must not lose the event
            logger.exception("live_send_failed_unexpectedly")
            self._go_offline()
            await self._append(event)
            return "spooled"

        self._online = True
        self._consecutive_failures = 0

        outcome = parse_batch_response(body).get(str(event.get("source_event_id")))
        if outcome is not None and outcome.accepted:
            return "delivered"
        if outcome is not None and outcome.permanently_rejected:
            logger.warning(
                "live_event_permanently_rejected",
                extra={"source_event_id": outcome.source_event_id, "code": outcome.error_code},
            )
            return "rejected"
        # Transient, or the server said nothing about it: spool it and let the drain
        # retry under the same policy every other row gets.
        await self._append(event)
        return "spooled"

    async def _append(self, event: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._spool.append, event)

    def _go_offline(self) -> None:
        if self._online:
            logger.warning("uplink_lost_events_are_being_spooled")
        self._online = False

    # --- The drain -------------------------------------------------------------------

    async def drain_once(self) -> DrainReport:
        """Delivers at most `max_batches_per_cycle` batches of the backlog, oldest first.

        Bounded on purpose: the cycle has to end so the loop can yield to the heartbeat,
        the command channel and fresh events. An unbounded drain would hold the uplink for
        the length of the whole backlog.
        """
        report = DrainReport()
        for _ in range(self._max_batches):
            batch = await asyncio.to_thread(self._spool.drain, self._batch_size)
            if not batch:
                break
            report.batches += 1
            try:
                await self._deliver(batch, report)
            except TransportError as exc:
                logger.warning("drain_interrupted", extra={"detail": str(exc)})
                report.link_failed = True
                self._go_offline()
                self._consecutive_failures += 1
                # Every row of this batch stays exactly where it was. A dropped connection
                # is not evidence about any of them.
                report.retained += len(batch)
                return report
            except Exception:  # noqa: BLE001 - a client bug must not delete anything
                logger.exception("drain_failed_unexpectedly")
                report.link_failed = True
                self._consecutive_failures += 1
                report.retained += len(batch)
                return report

        if report.batches:
            self._online = True
            self._consecutive_failures = 0
            logger.info(
                "spool_drain_cycle",
                extra={
                    "batches": report.batches, "delivered": report.delivered,
                    "duplicates": report.duplicates, "discarded": report.discarded,
                    "retained": report.retained, "depth": self._spool.depth(),
                },
            )
        return report

    async def _deliver(self, batch: Sequence[SpooledEvent], report: DrainReport) -> None:
        """Sends one batch and settles every row in it. Bisects on a batch-level refusal."""
        try:
            body = await self._send_fn([dict(row.event) for row in batch])
        except BatchRejected as exc:
            if len(batch) == 1:
                row = batch[0]
                logger.error(
                    "spool_row_permanently_undeliverable",
                    extra={
                        "source_event_id": row.event.get("source_event_id"),
                        "detail": exc.message,
                    },
                )
                await self._discard([row.id])
                report.discarded += 1
                return
            # Split and re-send. Replaying rows that may already have landed is safe - the
            # server dedupes on `source_event_id` - and is far cheaper than a spool that
            # can never drain past this batch.
            logger.warning(
                "batch_rejected_isolating", extra={"size": len(batch), "detail": exc.message}
            )
            middle = len(batch) // 2
            await self._deliver(batch[:middle], report)
            await self._deliver(batch[middle:], report)
            return

        outcomes = parse_batch_response(body)
        now = self._now()
        to_ack: list[int] = []
        to_discard: list[int] = []

        for row in batch:
            key = str(row.event.get("source_event_id"))
            outcome = outcomes.get(key)

            if outcome is not None and outcome.accepted:
                # Duplicates land here too, and that is the point: the server telling us it
                # already has the row is the strongest possible confirmation it holds it.
                to_ack.append(row.id)
                if outcome.duplicate:
                    report.duplicates += 1
                continue

            if outcome is not None and outcome.permanently_rejected:
                logger.error(
                    "spool_row_permanently_rejected",
                    extra={
                        "source_event_id": key, "code": outcome.error_code,
                        "camera_id": row.event.get("camera_id"),
                    },
                )
                to_discard.append(row.id)
                continue

            first_seen = self._rejections.note(row.id, now)
            if now - first_seen > self._rejection_deadline:
                logger.error(
                    "spool_row_abandoned_after_deadline",
                    extra={
                        "source_event_id": key,
                        "code": outcome.error_code if outcome else None,
                        "seconds_retried": round(now - first_seen),
                    },
                )
                to_discard.append(row.id)
                continue

            report.retained += 1

        if to_ack:
            await asyncio.to_thread(self._spool.ack, to_ack)
            self._rejections.forget(to_ack)
            report.delivered += len(to_ack)
        if to_discard:
            await self._discard(to_discard)
            report.discarded += len(to_discard)

    async def _discard(self, row_ids: Sequence[int]) -> None:
        """Deletes rows that will never be delivered, counted as drops.

        Counted, not merely deleted: `spool_dropped` is what carries this to the server on
        the next heartbeat, where it escalates the device to `degraded`. A discard that
        did not increment it would be exactly the silent loss the whole counter exists to
        prevent.
        """
        await asyncio.to_thread(self._spool.discard, row_ids)
        self._rejections.forget(row_ids)

    # --- The loop --------------------------------------------------------------------

    def next_delay_seconds(self, report: DrainReport) -> float:
        """How long to wait after the cycle `report` describes.

        Three cases, and the middle one is the one that matters:

          *The link failed* → exponential backoff with equal jitter, ceiling
          `backoff_max_seconds`. See the module docstring for the ceiling's justification.

          *The cycle made progress and there is still a backlog* → no wait. Continuing
          immediately is what lets a day's backlog clear in minutes rather than days, and
          it cannot spin: the cycle only counts as progress if rows actually left the
          spool, which costs a real round trip each time.

          *Anything else* → the idle interval. Notably this covers the dangerous case of a
          server that answers but rejects every row transiently: nothing shrinks, so the
          engine waits instead of hammering.
        """
        if report.link_failed:
            window = min(
                self._backoff_max,
                self._backoff_initial * (2 ** max(0, self._consecutive_failures - 1)),
            )
            # Equal jitter: half the window fixed, half random. The fixed half is the floor
            # that keeps a dead uplink from becoming a busy loop; the random half is what
            # stops a site's whole fleet reconnecting in lockstep.
            half = window / 2
            return half + self._rng.random() * half
        if report.progressed and self._spool.depth() > 0:
            return 0.0
        return self._idle_interval

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Drains until told to stop.

        The default wait races `stop`, rather than being a plain `asyncio.sleep`. That is
        not a nicety: the backoff ceiling is five minutes, and a device that had to serve
        out its current backoff before noticing SIGTERM would take up to five minutes to
        shut down - long past any container runtime's patience, so it would be killed
        mid-cycle instead of stopping cleanly.

        Every per-cycle failure is caught inside `drain_once`, so nothing ordinary escapes
        here. The blanket guard below is for the unanticipated: this loop runs alongside the
        heartbeat, and an exception escaping it would end the process and take the
        heartbeat with it - the device would go dark with no signal at all, which is
        strictly worse than a stalled drain that is still reporting its spool depth. Same
        reasoning, and the same asymmetry, as
        `notification_worker/app/main.py::_webhook_dispatch_never_takes_alerts_down_with_it`.
        """
        sleeper = sleep if sleep is not None else _make_interruptible_sleep(stop)
        while not stop.is_set():
            try:
                report = await self.drain_once()
            except asyncio.CancelledError:
                raise  # shutdown, not failure
            except Exception:  # noqa: BLE001 - see this method's own docstring
                logger.exception("sync_cycle_failed")
                self._consecutive_failures += 1
                report = DrainReport(link_failed=True)

            await sleeper(self.next_delay_seconds(report))


def _make_interruptible_sleep(stop: asyncio.Event) -> Callable[[float], Awaitable[None]]:
    """A sleep that ends early when `stop` is set."""

    async def wait(seconds: float) -> None:
        if seconds <= 0:
            # Still yields, so a zero-delay continue cannot starve the other loops on this
            # event loop - the heartbeat above all.
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    return wait
