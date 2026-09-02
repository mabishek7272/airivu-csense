"""A bounded ring buffer of the agent's own recent log records, folded into `health.logs`
on every heartbeat so a platform developer troubleshooting a device (self-service, or under
a `support_grants` elevation - see `diagnostic.read`, migration 0053) can see recent agent
activity without shell access to a box in a customer's building. No shell/exec exists in
this agent at all (see `main.py`'s module docstring on why only `ping` is implemented) -
this is the read-only substitute: the agent tells the platform what it has been doing,
rather than the platform reaching in to look.

**Why this is its own module rather than living in `main.py`.** It is self-contained state
(a deque and a running byte total) with two behaviours worth pinning in isolation - eviction
under a byte cap, and credential redaction - matching the convention this package already
follows: `spool.py`, `sync.py`, `source.py` and `crypto.py` are each one focused piece of
agent behaviour with its own test file. `main.py`'s own docstring is already dense with loop
-ordering reasoning that has nothing to do with log buffering; wiring this in is one call
(`handler.tail()`) in the heartbeat payload.

## The byte budget, worked out rather than picked

`HEALTH_PAYLOAD_LIMIT` (`tenant_api/app/api/edge.py`) is 16384 characters, checked against
the device's *own* submitted `health` dict before the server adds anything
(`_json(body.health, limit=HEALTH_PAYLOAD_LIMIT)`, checked ahead of the server's own
`spool` addition - see that file's comment on why bytes the device did not send must not
count against its own budget). So the log tail shares the device's 16384-character budget
with:
  - the server's own addition after the fact: the `spool` block (`depth`/`dropped`/
    `last_dropped_at`), pinned at roughly 62 bytes by
    `test_a_device_at_its_payload_budget_can_still_heartbeat_with_a_spool` in
    `test_edge_heartbeat_spool.py`;
  - this build's existing `service` block (`spool_bytes`, `uplink` - two keys, comfortably
    under 100 characters even with `spool_bytes` at its largest possible value,
    `CSENSE_SPOOL_MAX_BYTES`'s ceiling of 512 GiB as a ten-digit integer);
  - whatever `health` grows to carry next. It has already grown once this session (the
    `spool` block itself was new); connectivity diagnostics, quality/fps reporting, and
    other fields are named as open surface in this project's own `CLAUDE.md`. A budget that
    ate most of 16384 today would turn every future health field into a fight over the
    last few hundred bytes - the opposite of "real headroom".

`LOG_TAIL_BUDGET_BYTES = 4096` - one quarter of `HEALTH_PAYLOAD_LIMIT` - is the number
chosen. The arithmetic for the actual worst case:

    16384                                    HEALTH_PAYLOAD_LIMIT
    -  100                                   this build's `service` block (rounded up)
    -   62                                   the server's `spool` block (measured, above)
    - 4096                                   LOG_TAIL_BUDGET_BYTES (this module's cap)
    = 12126                                  bytes free for every health field not yet invented

A single `emit()` call is additionally capped at `MAX_MESSAGE_CHARS` so one absurdly long
line cannot alone approach - let alone exceed - the whole 4096-byte tail budget; the true
worst case per entry (see the arithmetic beside that constant) is a little over 600 bytes,
leaving room for several entries even at the moment a burst of maximally long lines arrives.
`test_edge_logbuf.py::test_full_log_buffer_and_spool_block_fit_health_payload_limit`
constructs exactly this worst case end to end (buffer filled to its cap, a realistic worst
-case `spool` block from the real `_spool_snapshot` function) and asserts the encoded
`health` dict is under `HEALTH_PAYLOAD_LIMIT` - a real regression test of this arithmetic,
not an assumption. This project shipped one near-miss on this exact budget already this
session (a test with 200 bytes of slack that would have passed with the bug present); this
one was mutation-checked instead (temporarily raising `LOG_TAIL_BUDGET_BYTES` to 20000 and
re-running that one test) - see that test's own docstring for the exact failure this
produced.

## Redaction is pattern-based, not value-based, on purpose

The handler is installed once, at process start (`main.py:amain`), before any credential
exists to compare a log line against - and it has to protect a log call site that might be
*added later*, which by definition it cannot have been given the literal secret to look
for. So it matches the *shape* every secret in this codebase shares - a long run of
base64url/hex characters *that also carries a randomness signal* - rather than one specific
value. Every credential this agent ever holds is such a string:
  - the long-lived `agent_token` issued at enrolment: `secrets.token_urlsafe(32)`
    (`tenant_api/app/api/edge.py::_generate_token`), 43 base64url characters;
  - `CSENSE_ENROLMENT_TOKEN` (`config.py`): opaque, operator-supplied, but drawn from the
    same generator server-side;
  - the device's own spool encryption key (`crypto.generate_device_key`): 32 raw bytes,
    which could only ever appear in a log line hex- or base64-encoded (64 or ~43 characters).

**A plain "20+ token characters" match is not enough - it swallows this package's own log
event names.** This package's `logger.*()` call sites name their events in
`snake_case_like_this`, and 22 of them (`device_spool_key_created`,
`detection_source_handler_failed`, `drain_failed_unexpectedly`, ...) are 20 characters or
longer - checked by walking every logger call's literal string argument across
`backend/edge_agent/app/`. A regex matching any 20+ character run of `[A-Za-z0-9_-]` would
redact the event name itself on most of this package's real log lines, which is a worse
failure than the one this module exists to prevent: not a leaked secret, but the tail
showing `"[redacted]"` instead of the one piece of information - *what happened* - every
entry is supposed to carry. `_looks_secret_shaped` is the fix: a run only counts as a secret
if it also contains a digit, a hyphen, or a mix of upper and lower case. Every real secret
in this codebase is close to uniformly random over its whole alphabet, so over 32+
characters it is overwhelmingly likely to show at least one of those three signals (a
64-character all-lowercase-letters hex digest with zero digits has probability roughly
(6/16)^64, i.e. never, in practice); this package's event names are deliberately plain
lowercase words joined by underscores and show none of them.

This is deliberately the *second* layer, not the only one: every existing `logger.*()` call
site in this package was grepped before writing this module and none logs `agent_token`,
`enrolment_token`, or the device key (see `test_edge_logbuf.py` for the grep this claim is
checked against in CI, not just asserted once by hand). The regex exists for the call site
that has not been written yet.

## Surfacing `extra` and `exc_info`, not just `getMessage()`

The first version of this module only read `record.getMessage()`. That was wrong in a way
that defeated the whole point of the tail: this package's own convention (every call site
grepped above) is to put the actual diagnostic detail in `extra={...}` and rely on
`logger.exception(...)`'s implicit traceback capture, not in the message string - `main.py`'s
own `%(message)s`-only console formatter never even prints `extra`, so a call site like
`logger.warning("heartbeat_failed", extra={"detail": str(exc)})` produced a buffered entry
reading only `"heartbeat_failed"`. A support session or self-service tenant reading that tail
to answer "why is this device unreachable" would see event names with the actual reason
stripped out - useless for Task 2's diagnostics endpoint.

So `emit()` now pulls a **small, explicit allowlist** of `extra` keys
(`ALLOWED_EXTRA_KEYS`) into the buffered message, chosen from what this package's call sites
actually attach (grepped, not guessed - see that constant's own comment for the specific
call sites each key comes from), and folds in a short summary of `record.exc_info` when
`logger.exception(...)` was used. Each piece goes through the *same* `_redact()` call the
base message already used, and - the detail worth being explicit about, since getting this
backwards silently reopens the credential leak this module exists to close - **every piece
is redacted before any truncation happens, and only the fully-assembled, fully-redacted
string is sliced to `MAX_MESSAGE_CHARS` at the end.** Truncating first and redacting after
would let a secret get cut in half at the truncation boundary, dropping it below the 20
-character run `_SECRET_LIKE` needs to match and letting the surviving fragment through
unredacted - the ordering is the only thing standing between "a truncated secret" and "an
unredacted one".
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

# See the module docstring's "byte budget" section for the arithmetic behind this number.
LOG_TAIL_BUDGET_BYTES = 4096

# Bounds the *final assembled* message - base text plus any allowed `extra` values plus any
# `exc_info` summary (see `ALLOWED_EXTRA_KEYS` and `emit()` below) - so one absurdly long
# piece cannot alone consume (or, under a tight enough budget, exceed) the whole tail. 512
# characters is generous for this codebase's actual messages: static event names
# (`"heartbeat_failed"`, `"command_expired_not_executed"`, ...) are a handful of characters,
# `MAX_EXTRA_VALUE_CHARS` already bounds each individual `extra`/`exc_info` piece to 200, and
# there are at most 5 allowed keys plus one exception summary - so the assembled string
# rarely needs truncating in practice, and this cap exists for the pathological case (a
# single `extra` value already at 200 chars from several keys at once) rather than the
# common one. The true worst case per entry is roughly 512 (message) + 32 (ISO timestamp
# with microseconds) + 8 ("CRITICAL") + ~50 (JSON structure: quotes, colons, commas, braces)
# = ~600 bytes, well under `LOG_TAIL_BUDGET_BYTES` on its own, which is what guarantees the
# eviction loop below always converges.
MAX_MESSAGE_CHARS = 512

# Candidate runs worth checking for the randomness signal - see the module docstring's
# redaction section for why a bare length check is not enough on its own (it swallows this
# package's own `snake_case` event names, 22 of which are 20+ characters). 20 characters is
# short enough to catch a truncated fragment of a longer secret and long enough that it does
# not collide with short scalar `extra` values (`command_id`, `code`, ...).
_CANDIDATE_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")


def _looks_secret_shaped(candidate: str) -> bool:
    """The randomness signal that separates a real secret from a long, plain identifier.

    Every credential this agent holds (`secrets.token_urlsafe` output, a hex-encoded key -
    see the module docstring) is close to uniformly random over its full alphabet, so over
    20+ characters it is overwhelmingly likely to contain a digit, a hyphen, or a case
    change. This package's own log event names are deliberately plain lowercase words
    joined by underscores and show none of the three - the cheapest signal that reliably
    tells them apart without knowing the literal secret value in advance.
    """
    return (
        any(ch.isdigit() for ch in candidate)
        or "-" in candidate
        or (any(ch.isupper() for ch in candidate) and any(ch.islower() for ch in candidate))
    )


def _redact(message: str) -> str:
    return _CANDIDATE_TOKEN.sub(
        lambda m: "[redacted]" if _looks_secret_shaped(m.group()) else m.group(), message
    )


# The `extra={...}` keys worth surfacing in a diagnostic log tail - chosen by grepping every
# `logger.*()` call site in this package (`crypto.py`, `main.py`, `source.py`, `spool.py`,
# `sync.py` - the same sweep `test_edge_logbuf.py`'s AST-based test checks for a leaked
# credential) for what it actually attaches, not picked in the abstract:
#   - `detail`: the exception text on every uplink/ack failure - `heartbeat_failed`,
#     `command_poll_failed`, `command_ack_failed` (`main.py`), `drain_interrupted`
#     (`sync.py`), `agent_cannot_start` (`main.py`). This is the single highest-value key:
#     it is the actual reason a device looks unreachable or degraded.
#   - `code`: the server's rejection/error code for one delivery outcome
#     (`sync.py`'s `_note_rejection`/`_deliver_bisected`/retry-accounting call sites).
#   - `command_id` / `command_type`: which command expired, was unsupported, or ran
#     (`main.py`'s `_run_command`/`command_loop`).
#   - `source_event_id`: which spooled event a rejection or retry refers to (`sync.py`).
# Deliberately not every key that exists (`path`, `mode`, `bytes`, `authenticated`,
# `batches`/`delivered`/`duplicates`/`discarded`/`retained`, ...): those either identify the
# box/environment rather than explain a failure, duplicate what the server's own `spool`
# block already reports (eviction depth/count), or are drain-cycle summaries with no single
# failure to point at. A wider allowlist is also more surface for a future call site to
# accidentally attach something sensitive under one of these key names - small and
# deliberate is the point, not exhaustive.
ALLOWED_EXTRA_KEYS = frozenset({"detail", "code", "command_id", "command_type", "source_event_id"})

# Caps each individual `extra` value (and the `exc_info` summary) before they are joined
# into the message, so one oversized value cannot alone crowd out the base message or every
# other allowed key - independent of, and in addition to, the final `MAX_MESSAGE_CHARS` cap
# applied to the fully assembled string.
MAX_EXTRA_VALUE_CHARS = 200


class RingBufferLogHandler(logging.Handler):
    """Keeps this process's most recent log records, capped by total encoded byte size
    rather than entry count - a burst of long lines has to evict older entries to make
    room, not silently grow past the budget just because it stayed under some row ceiling.

    Thread-safe: `logging` can call `emit()` from any thread a handler is attached in, and
    `tail()` is read from the heartbeat loop concurrently with `emit()` calls from whichever
    thread logs. A single lock around both keeps the byte accounting honest; the buffer is
    small and `emit()`/`tail()` are cheap, so contention here is not a real concern.
    """

    def __init__(self, budget_bytes: int = LOG_TAIL_BUDGET_BYTES) -> None:
        super().__init__()
        self._budget_bytes = budget_bytes
        self._entries: deque[dict[str, Any]] = deque()
        self._costs: deque[int] = deque()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # A logging handler must never raise - `logging` swallows what `emit()` throws only
        # inconsistently, and a broken handler taking down the heartbeat loop over a
        # malformed log call would be a self-inflicted version of exactly the failure mode
        # this module exists to help diagnose.
        try:
            # Every piece is redacted *before* it is joined or truncated - see the module
            # docstring's "Surfacing extra and exc_info" section for why that ordering, not
            # the reverse, is what keeps a truncated secret from slipping past `_SECRET_LIKE`.
            pieces = [_redact(record.getMessage())]

            for key in sorted(ALLOWED_EXTRA_KEYS):
                value = record.__dict__.get(key)
                if value is not None:
                    pieces.append(f"{key}={_redact(str(value))[:MAX_EXTRA_VALUE_CHARS]}")

            if record.exc_info:
                exc_type, exc_value, _tb = record.exc_info
                if exc_type is not None:
                    summary = f"{exc_type.__name__}: {exc_value}"
                    pieces.append(_redact(summary)[:MAX_EXTRA_VALUE_CHARS])

            message = " | ".join(pieces)[:MAX_MESSAGE_CHARS]
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "message": message,
            }
            # The real encoded size, matching how `tenant_api/app/api/edge.py::_json`
            # measures the budget it enforces (character count of `json.dumps(...)`, not a
            # separate UTF-8 byte count) - so this handler's own bookkeeping and the
            # server's enforcement agree on what "size" means.
            cost = len(json.dumps(entry))
        except Exception:  # noqa: BLE001 - see this method's own comment above
            return

        with self._lock:
            self._entries.append(entry)
            self._costs.append(cost)
            self._total_bytes += cost
            # `> 1`, not `> 0`: the most recent entry is kept even if it alone exceeds the
            # budget, so a pathologically small budget degrades to "shows only the latest
            # line" rather than "shows nothing forever". This never actually triggers at
            # the production budget - `MAX_MESSAGE_CHARS` bounds any one entry to roughly
            # 600 bytes, well under `LOG_TAIL_BUDGET_BYTES` - so the ordinary case is a
            # strict cap; this is the graceful edge, not the normal path.
            while self._total_bytes > self._budget_bytes and len(self._entries) > 1:
                self._entries.popleft()
                self._total_bytes -= self._costs.popleft()

    def tail(self) -> list[dict[str, Any]]:
        """A snapshot safe to embed directly under `health["logs"]` - oldest first, matching
        how an operator reads a log."""
        with self._lock:
            return list(self._entries)
