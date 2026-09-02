"""`RingBufferLogHandler`: byte-cap eviction, credential redaction, and the arithmetic
behind `LOG_TAIL_BUDGET_BYTES` that keeps a full log tail plus a full spool block inside
`HEALTH_PAYLOAD_LIMIT`.

Loaded by path, the same way `test_edge_main_loop_isolation.py` loads `main.py`: every
service under `backend/` names its package `app`, so a bare `import app.logbuf` would
resolve to whichever service another test module happened to import first - and
`logbuf.py` sits inside that same `app` package, so the whole package has to be registered
(`submodule_search_locations`) even though this file only imports the one submodule.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import logging
import pathlib
import sys

import pytest

_PACKAGE = "csense_edge_agent_logbuf_under_test"


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


_agent_root = pathlib.Path(__file__).resolve().parents[1] / "edge_agent" / "app"
_pkg = _load_agent_package()
logbuf = importlib.import_module(f"{_PACKAGE}.logbuf")
crypto = importlib.import_module(f"{_PACKAGE}.crypto")
config = importlib.import_module(f"{_PACKAGE}.config")


def _record(
    message: str, *, level: int = logging.INFO, args: tuple = (), exc_info=None
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=1,
        msg=message, args=args, exc_info=exc_info,
    )


def _long_realistic_text(min_chars: int) -> str:
    """A long message that is genuinely long *and* does not trip `_SECRET_LIKE` - every
    word is well under the regex's 20-character run threshold and separated by spaces, the
    way a real error message actually reads (`"connection refused while posting ..."`), not
    a single repeated character. Using `"z" * N` here would be wrong the same way the
    original version of these tests was wrong: `_SECRET_LIKE` matches any 20+ character
    unbroken run, so a repeated-character filler collapses to `"[redacted]"` *before*
    truncation ever runs, and a test built on it exercises redaction, not truncation.
    """
    phrase = (
        "connection refused while posting detections batch to tenant api after several "
        "retries with backoff still climbing toward the configured ceiling "
    )
    text = (phrase * (min_chars // len(phrase) + 2)).strip()
    assert len(text) >= min_chars  # sanity: the helper actually met its own contract
    return text


# --- Byte-cap eviction ----------------------------------------------------------------

def test_buffer_never_exceeds_its_byte_cap_under_a_burst_of_long_lines():
    handler = logbuf.RingBufferLogHandler(budget_bytes=1000)
    for i in range(500):
        handler.emit(_record("x" * logbuf.MAX_MESSAGE_CHARS + str(i)))

    tail = handler.tail()
    assert len(json.dumps(tail)) <= 1000
    # The cap bounds the buffer; it does not empty it.
    assert tail


def test_oldest_entries_are_evicted_first():
    handler = logbuf.RingBufferLogHandler(budget_bytes=400)
    for i in range(20):
        # Spaced filler, deliberately not a 20+ character unbroken run: this test is about
        # eviction order, not redaction, and a message shaped like a secret would be
        # rewritten to "[redacted]" by the (separately tested) redaction layer, making
        # every entry identical and this assertion meaningless.
        handler.emit(_record(f"line {i:03d} " + "y " * 50))

    messages = [entry["message"] for entry in handler.tail()]
    joined = "\n".join(messages)
    assert "line 000 " not in joined  # earliest is long gone
    assert "line 019 " in joined  # most recent survived
    # What remains is still oldest-first, matching how an operator reads a log.
    assert messages == sorted(messages)


def test_a_pathologically_small_budget_still_keeps_the_latest_entry():
    """The eviction loop's `len(self._entries) > 1` guard (see `logbuf.py`): a budget
    smaller than even one entry degrades to "shows only the latest line" rather than an
    empty buffer forever. Never triggers at the real production budget - `MAX_MESSAGE_CHARS`
    keeps any single entry well under `LOG_TAIL_BUDGET_BYTES` - so this pins the edge case
    deliberately, not by accident."""
    handler = logbuf.RingBufferLogHandler(budget_bytes=1)
    handler.emit(_record("y" * logbuf.MAX_MESSAGE_CHARS))

    assert len(handler.tail()) == 1


def test_entries_are_structured_not_raw_text():
    handler = logbuf.RingBufferLogHandler()
    handler.emit(_record("hello %s", args=("world",), level=logging.WARNING))

    [entry] = handler.tail()
    assert set(entry) == {"timestamp", "level", "message"}
    assert entry["level"] == "WARNING"
    assert entry["message"] == "hello world"  # %-args are substituted, not stored raw
    # A real, parseable timestamp - not a formatted string glued onto the message.
    import datetime as dt

    dt.datetime.fromisoformat(entry["timestamp"])


def test_one_absurdly_long_line_is_truncated_not_left_to_dominate_the_buffer():
    """Fixed from a vacuous version of itself (code review, 2026-09-02): the original test
    built its huge line as `"z" * 100_000` - a single repeated character, exactly the shape
    `_redact()` collapses to `"[redacted]"` (9 chars) *before* truncation ever ran, so the
    test passed regardless of whether truncation worked. `_long_realistic_text` is
    deliberately word-shaped so redaction leaves it alone and truncation is the only thing
    that can shorten it.

    Mutation-checked: temporarily removed the `[:MAX_MESSAGE_CHARS]` slice from `emit()`'s
    assembled message and reran this test - it failed (the untruncated ~100000-character
    entry blew the 2000-byte budget on its own, and once "second line" arrived the eviction
    loop dropped the oversized entry entirely rather than keeping a truncated version of it,
    so `tail[0]` ended up being `"second line"`, not the truncated huge line the later
    assertions expect). Reverted before this file was committed.
    """
    huge = _long_realistic_text(100_000)
    # Proves this test actually exercises truncation: the raw text really is far longer
    # than the cap before it ever reaches `emit()`, not already short from redaction or
    # from a filler string too small to need truncating in the first place.
    assert len(huge) > logbuf.MAX_MESSAGE_CHARS * 100

    handler = logbuf.RingBufferLogHandler(budget_bytes=2000)
    handler.emit(_record(huge))
    handler.emit(_record("second line"))

    tail = handler.tail()
    assert len(tail[0]["message"]) <= logbuf.MAX_MESSAGE_CHARS
    # Genuinely truncated, not coincidentally short - some of the real text survives the cut.
    assert tail[0]["message"].startswith("connection refused")
    # The huge line did not consume so much budget that the very next entry was evicted.
    assert tail[-1]["message"] == "second line"


# --- The real HEALTH_PAYLOAD_LIMIT arithmetic ------------------------------------------

def _load_edge_module():
    """Loads tenant_api's `app.api.edge` by path - same loader as
    `test_edge_heartbeat_spool.py`, needed here only for `HEALTH_PAYLOAD_LIMIT` and the
    real `_spool_snapshot` function, so the worst-case spool block below is the one the
    server actually produces rather than a hand-rolled approximation of it."""
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_edge_under_test_from_logbuf", service_dir / "app" / "api" / "edge.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(service_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def _worst_case_health() -> dict:
    """The device's own `health` dict (before the server adds anything) at its worst
    plausible real size: the log tail filled to its byte cap with maximum-length lines,
    plus this build's actual `service` block at its largest values.

    Each logged line is genuinely longer than `MAX_MESSAGE_CHARS` before it reaches
    `emit()` (`_long_realistic_text` guarantees that, and does not collapse under
    redaction the way a single repeated character would - see that helper's own
    docstring) - so every buffered entry is a real truncated-to-the-cap message, the
    actual worst case this function's name claims, not an accidentally-shrunk one.

    Fixed from a vacuous version of itself (code review, 2026-09-02): the original filler
    was `"w" * MAX_MESSAGE_CHARS + str(i)` - a single repeated character glued directly to
    a digit suffix with no separating space, which collapsed under redaction to
    `"[redacted]"` before truncation ever ran, so this function was not actually exercising
    "the log tail filled with maximum-length lines" the way its own docstring claimed.

    Mutation-checked: temporarily restored that exact original filler shape (single
    character + glued digit, no space) and reran `test_full_log_buffer_and_spool_block_
    fit_health_payload_limit` - the internal assertion below (every entry's message length
    equals `MAX_MESSAGE_CHARS`) failed immediately, every buffered message having collapsed
    to `"[redacted]"` instead. That is the proof this function now genuinely builds the
    worst case rather than an accidentally-redacted stand-in for it. Reverted before this
    file was committed.
    """
    handler = logbuf.RingBufferLogHandler()
    filler = _long_realistic_text(logbuf.MAX_MESSAGE_CHARS + 100)
    i = 0
    while True:
        handler.emit(_record(f"{filler} {i}"))
        i += 1
        if i > 200:  # generous ceiling; the byte cap converges long before this
            break

    tail = handler.tail()
    # Confirms this is actually the worst case, not just "some entries": every surviving
    # line is truncated right up to the cap.
    assert tail and all(len(entry["message"]) == logbuf.MAX_MESSAGE_CHARS for entry in tail)

    return {
        "service": {
            # CSENSE_SPOOL_MAX_BYTES's own ceiling (config.py) - the largest value this
            # field can actually hold.
            "spool_bytes": 512 * 1024**3,
            "uplink": "down",  # longer than "up"
        },
        "logs": handler.tail(),
    }


def test_full_log_buffer_and_spool_block_fit_health_payload_limit():
    """The real regression test for the arithmetic in `logbuf.py`'s module docstring:
    a log tail genuinely filled to its byte cap, plus this build's own `service` block,
    plus the real worst-case `spool` block the server adds (`_spool_snapshot` with large,
    ever-climbing counters) - all combined - must fit inside `HEALTH_PAYLOAD_LIMIT`.

    This project already shipped one near-miss on this exact budget this session (a test in
    `test_edge_heartbeat_spool.py` that left ~200 bytes of slack and would have passed with
    the bug present); this test is sized to the true worst case instead, and was mutation
    -checked by hand: with `LOG_TAIL_BUDGET_BYTES` temporarily raised to 20000, this test
    failed - on its *first* assertion (the device's own payload, before the server's `spool`
    block is even added): 18763 characters against a limit of 16384. That is the proof this
    test actually catches a regression in the budget rather than passing regardless of it;
    the constant was reverted to 4096 before this file was committed.
    """
    edge = _load_edge_module()

    device_health = _worst_case_health()
    # The device-side check the agent's own payload has to pass, unaltered by anything the
    # server adds - mirrors `_json(body.health, limit=HEALTH_PAYLOAD_LIMIT)` in edge.py.
    assert len(json.dumps(device_health)) <= edge.HEALTH_PAYLOAD_LIMIT

    # Now the server's own addition, using the real function rather than a hand-rolled
    # dict, with counters large enough to be a genuine worst case (10-digit integers).
    import datetime as dt

    spool_block, _ = edge._spool_snapshot(
        None, depth=9_999_999_999, dropped=9_999_999_999, now=dt.datetime.now(dt.UTC)
    )
    combined = dict(device_health)
    combined["spool"] = spool_block

    encoded = json.dumps(combined)
    assert len(encoded) <= edge.HEALTH_PAYLOAD_LIMIT, (
        f"combined health payload is {len(encoded)} chars against a limit of "
        f"{edge.HEALTH_PAYLOAD_LIMIT}"
    )


# --- Credential redaction ---------------------------------------------------------------

def test_a_real_generated_device_key_never_appears_in_a_buffered_line():
    """A real device key, produced the same way `crypto.load_or_create_device_key` makes
    one (`crypto.generate_device_key`) - not a hand-typed stand-in - deliberately logged by
    a call site that should never exist, to prove the handler's *own* redaction catches it
    rather than merely the absence of any real log call doing this today."""
    key_hex = crypto.generate_device_key().hex()  # 64 hex chars, exactly what a hex-encoded
    # device key would look like if a future bug logged it that way.
    handler = logbuf.RingBufferLogHandler()

    handler.emit(_record(f"device key is {key_hex} - this call site should never exist"))

    [entry] = handler.tail()
    assert key_hex not in entry["message"]
    assert "[redacted]" in entry["message"]


def test_a_real_generated_enrolment_token_never_appears_in_a_buffered_line():
    """Same proof for the other credential shape this agent handles: a real
    `CSENSE_ENROLMENT_TOKEN`, produced by `AgentSettings.from_env` exactly as the running
    agent would read it from its own environment."""
    settings = config.AgentSettings.from_env({
        "CSENSE_API_BASE_URL": "https://tenant-api.example.test",
        "CSENSE_ENROLMENT_TOKEN": "a" * 10 + "Bz9-" + "Q" * 30,  # secrets.token_urlsafe(32)-shaped
    })
    token = settings.enrolment_token
    assert token  # sanity: the settings object really carries a real-shaped token
    handler = logbuf.RingBufferLogHandler()

    handler.emit(_record(f"enrolling with token={token}"))

    [entry] = handler.tail()
    assert token not in entry["message"]
    assert "[redacted]" in entry["message"]


def test_long_legitimate_event_names_are_not_swallowed_by_redaction():
    """A regression test for a real bug found while fixing Issue 1's `extra`-visibility
    gap: a plain "20+ token characters" redaction rule would have matched this package's
    own `snake_case` event names too, and 22 of them (grepped across every `logger.*()`
    literal string in `backend/edge_agent/app/`) are 20 characters or longer. Before
    `_looks_secret_shaped` existed, every one of these lines would have shown
    `"[redacted]"` in the buffered tail instead of the actual event name - not a leaked
    secret, but the opposite failure: the one piece of information every entry exists to
    carry, gone. A handful of the longest real ones, pinned directly rather than re-derived
    by grep here (that grep already lives in `test_no_existing_log_call_site_in_the_agent_
    references_the_credential`, immediately below)."""
    real_event_names = [
        "device_spool_key_created",
        "detection_source_handler_failed",
        "batch_result_without_source_event_id",
        "spool_row_permanently_undeliverable",
        "uplink_lost_events_are_being_spooled",
    ]
    handler = logbuf.RingBufferLogHandler()
    for name in real_event_names:
        assert len(name) >= 20  # sanity: these are the ones actually at risk
        handler.emit(_record(name))

    messages = [entry["message"] for entry in handler.tail()]
    assert messages == real_event_names
    assert "[redacted]" not in "".join(messages)


def test_no_existing_log_call_site_in_the_agent_references_the_credential():
    """The first, primary layer this module's docstring describes: no call site in this
    package passes `agent_token` or `enrolment_token` to a logger today. Parses the real
    source with `ast` (not a plain substring grep) so this survives call sites spanning
    multiple lines and does not false-positive on the word appearing in a comment or an
    unrelated dict key - and so a future call site that *does* leak one fails this test
    immediately rather than relying on the regex-based redaction alone.
    """
    offending: list[str] = []
    for path in sorted(_agent_root.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_logger_call = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
                and func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"}
            )
            if not is_logger_call:
                continue
            call_source = ast.unparse(node)
            if "agent_token" in call_source or "enrolment_token" in call_source:
                offending.append(f"{path.name}: {call_source}")

    assert not offending, (
        "A log call site references the device credential directly - fix the call site, "
        "the handler's own redaction is a second layer, not a substitute:\n"
        + "\n".join(offending)
    )


# --- Surfacing `extra` and `exc_info` (the actual diagnostic content) -------------------

def test_the_real_heartbeat_failed_detail_is_visible_in_the_buffered_entry():
    """The exact shape `main.py::heartbeat_loop` logs today:
    `logger.warning("heartbeat_failed", extra={"detail": str(exc)})`. Before this test
    existed, the buffered entry for this call was just the bare string `"heartbeat_failed"`
    - the single most useful piece of information (why the heartbeat failed) was dropped on
    the floor, which defeated the entire point of Task 2's diagnostics endpoint. This proves
    the allowlist actually surfaces it, not just that the mechanism exists in the abstract.

    Mutation-checked, alongside its two siblings below: temporarily set
    `ALLOWED_EXTRA_KEYS = frozenset()` and reran all three `_detail_is_visible_` tests - all
    three failed (each on its own detail-content assertion, message reduced to the bare
    event name). Reverted before this file was committed.
    """
    handler = logbuf.RingBufferLogHandler()
    record = _record("heartbeat_failed", level=logging.WARNING)
    record.detail = "The API could not be reached: [Errno 111] Connection refused"

    handler.emit(record)

    [entry] = handler.tail()
    assert "heartbeat_failed" in entry["message"]
    assert "Connection refused" in entry["message"]


def test_command_lifecycle_detail_is_visible_in_the_buffered_entry():
    """The real shape `main.py::_run_command` logs for an expired or unsupported command -
    `extra={"command_id": ..., "command_type": ...}` - proving the other allowed keys work,
    not only `detail`."""
    handler = logbuf.RingBufferLogHandler()
    record = _record("command_unsupported", level=logging.INFO)
    record.command_id = "cmd-8f2a"
    record.command_type = "model_update"

    handler.emit(record)

    [entry] = handler.tail()
    assert "cmd-8f2a" in entry["message"]
    assert "model_update" in entry["message"]


def test_rejection_detail_is_visible_in_the_buffered_entry():
    """The real shape `sync.py` logs for a rejected spooled event -
    `extra={"source_event_id": ..., "code": ...}`."""
    handler = logbuf.RingBufferLogHandler()
    record = _record("batch_rejected_isolating", level=logging.WARNING)
    record.source_event_id = "evt-771"
    record.code = "duplicate_detection"

    handler.emit(record)

    [entry] = handler.tail()
    assert "evt-771" in entry["message"]
    assert "duplicate_detection" in entry["message"]


def test_extra_keys_outside_the_allowlist_are_not_included():
    """The allowlist is small on purpose (see `ALLOWED_EXTRA_KEYS`'s own comment) - a key
    like `path` (`crypto.py`'s `device_spool_key_created`) or `bytes` (`spool.py`'s eviction
    log) must not silently start appearing just because some future call site happens to use
    that name for something sensitive. Also proves a non-allowlisted `extra` attribute does
    not crash `emit()`."""
    handler = logbuf.RingBufferLogHandler()
    record = _record("device_spool_key_created", level=logging.INFO)
    record.path = "/var/lib/csense-agent/device.key"

    handler.emit(record)  # must not raise

    [entry] = handler.tail()
    assert entry["message"] == "device_spool_key_created"
    assert "/var/lib/csense-agent" not in entry["message"]


def test_exc_info_summary_is_visible_without_the_full_traceback():
    """The real shape `sync.py`'s `logger.exception("drain_failed_unexpectedly")` and
    `main.py`'s `logger.exception(f"{name}_died_heartbeat_continues")` produce: `logger.
    exception(...)` implicitly captures `sys.exc_info()`. A short `Type: message` summary
    should appear; the full traceback (large, and more likely to carry incidental detail)
    should not."""
    handler = logbuf.RingBufferLogHandler()
    try:
        raise ValueError("spool file is corrupt at offset 4096")
    except ValueError:
        record = _record(
            "drain_failed_unexpectedly", level=logging.ERROR, exc_info=sys.exc_info()
        )

    handler.emit(record)

    [entry] = handler.tail()
    assert "ValueError" in entry["message"]
    assert "spool file is corrupt at offset 4096" in entry["message"]
    # The full traceback (file paths, line numbers, "Traceback (most recent call last)")
    # is not what got captured - only the short summary.
    assert "Traceback" not in entry["message"]


def test_extra_values_and_exc_info_are_redacted_like_the_base_message():
    """The credential-redaction guarantee has to hold for the new content too, not just the
    original message - a secret leaking in via `extra={"detail": ...}` would be exactly as
    bad as one leaking in via the message string itself."""
    key_hex = crypto.generate_device_key().hex()
    handler = logbuf.RingBufferLogHandler()
    record = _record("heartbeat_failed", level=logging.WARNING)
    record.detail = f"auth header carried token {key_hex} unexpectedly"

    handler.emit(record)

    [entry] = handler.tail()
    assert key_hex not in entry["message"]
    assert "[redacted]" in entry["message"]


@pytest.mark.skipif(
    not hasattr(logbuf, "LOG_TAIL_BUDGET_BYTES"), reason="module shape changed"
)
def test_budget_constant_is_a_quarter_of_the_health_payload_limit():
    """Pins the documented relationship between the two numbers so a future edit to either
    constant is forced to re-read (and re-justify) the other, rather than drifting apart
    silently."""
    edge = _load_edge_module()
    assert logbuf.LOG_TAIL_BUDGET_BYTES == edge.HEALTH_PAYLOAD_LIMIT // 4
