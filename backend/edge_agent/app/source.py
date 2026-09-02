"""Where detections come from — and, just as importantly, where they do not.

**This agent does not do inference.** It has no model, no video decoder and no camera
connection, and nothing in this module fakes one. A detection reaches the agent because
something else on the box produced it: a co-located inference process (the platform's
existing `ai-runtime`, or a customer's own) posting to the small HTTP listener below.
Running models on ARM edge hardware is a larger, separate piece of work that belongs with
the Phase 4 AI programme; the plan for this change (`docs/superpowers/plans/
2026-09-02-edge-agent-offline-spool.md`, "Decisions already made") says so explicitly, and
says why a placeholder detector would be worse than none: a module here called `detector`
that emitted plausible-looking events would make a deployment *look* like it was watching
the site. For a safety product that is the single worst thing a stub can do — nobody
investigates alerts that are arriving.

So the source is an interface with exactly one honest implementation.

**Why HTTP over a socket, a pipe or a shared queue.** The producer is a separate process,
frequently in a separate container, sometimes not written by us and sometimes not written
in Python. HTTP is the one interface every one of those can already speak, and it is the
*same* shape as `POST /api/v1/tenant/ingest/detections` — so the inference side has one
detection format to produce, whether it is posting to the cloud directly or handing events
to this agent to buffer. A bespoke IPC channel would buy a little throughput at the price
of a second format nobody else implements.

**Binding.** Loopback by default, and `config.py` refuses to bind wider without a shared
token — see `AgentSettings.listen_host` for the reasoning. Restated in one line: every
event accepted here can become an incident and a 3am phone call, so an unauthenticated
listener on a site LAN is an incident generator reachable by the least trustworthy devices
on that network (the cameras themselves).

**Validation is deliberately shallow, but not absent.** The server is the authority on
what a valid detection is, and duplicating its rules here would guarantee the two drift.
What this module checks is the narrower question of whether an event can *survive the
round trip at all*: the fields the spool itself needs (`captured_at`, above all — the spool
will not invent one), and the handful of shapes that would make a row permanently
undeliverable once it is sealed in the spool. Catching those at the door returns a 422 to
the producer at the moment a human can still fix it, instead of turning into a silently
discarded row a day later. Everything else is passed through and judged by the server.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# One event's JSON. Generous enough for a full-size base64 frame (the API's own
# MAX_FRAME_BYTES is 8 MiB, ~10.7 MiB base64-encoded) and bounded so a producer looping on
# a bug cannot make the agent buffer without limit. `crypto.MAX_ROW_BYTES` is the matching
# ceiling one step further in; this one is deliberately a little smaller so an oversized
# event is refused with an explanation rather than accepted and then rejected by the spool.
MAX_BODY_BYTES = 12 * 1024 * 1024

# Mirrors `MAX_OBJECTS_PER_FRAME` in `csense_shared.pipeline.ingest`. Restated rather than
# imported for the reason this whole package restates things (see `app/__init__.py`); if
# the server's limit changes, the worst this costs is an event refused here that the server
# would have taken, which is visible immediately rather than silent.
MAX_OBJECTS = 100

# The fields the agent forwards. Anything else a producer sends is dropped rather than
# spooled: unknown keys cost encrypted disk on a device with very little of it, and the
# server ignores them anyway. `edge_device_id` is deliberately *not* here - the device's
# own credential establishes it server-side, and a value in the body would be a device
# claiming to report on another device's behalf.
_FORWARDED_FIELDS = (
    "camera_id",
    "source_event_id",
    "captured_at",
    "objects",
    "frame_base64",
    "model_version_id",
)


class SourceError(ValueError):
    """The submitted event cannot be accepted. The message is shown to the producer."""


# What the agent does with an accepted event. Returns the outcome string the sync engine
# produced ("delivered", "spooled", "rejected"), which is echoed back to the producer -
# a source that knows its event was spooled rather than delivered can say so in its own
# logs, and that is often the first clue anyone has that a site is offline.
EventHandler = Callable[[Mapping[str, Any]], Awaitable[str]]


class DetectionSource(Protocol):
    """Anything that hands detections to the agent.

    Two methods, on purpose. A source owns a resource (a socket, a subscription, a file
    watch) and the agent has to be able to shut it down cleanly on SIGTERM, or a restart
    leaves the listen port held by a dying process.
    """

    async def start(self) -> None:
        """Begins accepting events. Must return promptly, not block."""

    async def aclose(self) -> None:
        """Stops accepting events and releases whatever `start` acquired."""


class LocalHttpSource:
    """A loopback HTTP listener speaking the same detection shape the API takes.

    `POST /detections`  — one detection object, or `{"detections": [...]}` for several.
    `GET  /healthz`     — liveness for a container healthcheck. Unauthenticated, and says
                          nothing but "the listener is up": a health endpoint that leaked
                          spool depth would tell anything that can reach the port whether
                          the site is currently offline.

    Threading, not asyncio, because `http.server` is what the standard library gives us and
    writing an HTTP parser to avoid a thread would be a bad trade. Each request thread hands
    its event to the agent's event loop via `run_coroutine_threadsafe` and waits for the
    outcome, so the producer gets a truthful answer ("spooled", not "delivered") rather than
    a fire-and-forget 202 that would hide an outage from the one process best placed to
    report it.
    """

    def __init__(
        self,
        handler: EventHandler,
        *,
        host: str = "127.0.0.1",
        port: int = 8099,
        token: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        handler_timeout_seconds: float = 30.0,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._token = token
        self._loop = loop
        self._handler_timeout = handler_timeout_seconds
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port, which is the requested one unless 0 was asked for (tests do)."""
        return self._server.server_address[1] if self._server else self._port

    async def start(self) -> None:
        if self._server is not None:
            raise SourceError("This source is already running.")
        loop = self._loop or asyncio.get_running_loop()
        self._server = _build_server(
            (self._host, self._port),
            handler=self._handler,
            token=self._token,
            loop=loop,
            timeout=self._handler_timeout,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="csense-detection-source",
            # Daemon so a hung request thread can never hold the process open past a
            # SIGTERM. `aclose` shuts the server down properly; this is the backstop.
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "detection_source_listening",
            extra={"host": self._host, "port": self.port, "authenticated": bool(self._token)},
        )

    async def aclose(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        # `shutdown` blocks until the serve loop exits, and it must not be called from the
        # serve thread itself - it is called here from the event loop's thread, so it goes
        # to a worker to avoid stalling every other loop for the duration.
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)


def normalise_event(payload: Any) -> dict[str, Any]:
    """Validates the shallow structural facts and returns the event the agent will spool.

    Raises `SourceError` with a message meant for whoever wrote the producer. See the
    module docstring for why this is not a re-implementation of the server's validation.
    """
    if not isinstance(payload, dict):
        raise SourceError("A detection must be a JSON object.")

    camera_id = payload.get("camera_id")
    if not isinstance(camera_id, str) or not camera_id.strip():
        raise SourceError("camera_id is required and must be a string.")

    source_event_id = payload.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id.strip():
        # The idempotency key the server dedupes on. Without it, a replayed spool row
        # becomes a second detection and a second incident - the exact failure the whole
        # at-least-once design exists to avoid - so it is never defaulted here.
        raise SourceError("source_event_id is required: it is the key the server dedupes on.")
    if len(source_event_id) > 200:
        raise SourceError("source_event_id must be at most 200 characters.")

    captured_at = payload.get("captured_at")
    if isinstance(captured_at, dt.datetime):
        captured_at = captured_at.isoformat()
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise SourceError("captured_at is required, as an ISO-8601 timestamp.")
    try:
        dt.datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise SourceError(f"captured_at '{captured_at}' is not an ISO-8601 timestamp.") from exc

    objects = payload.get("objects")
    if not isinstance(objects, list):
        raise SourceError("objects must be a list, even when it is empty.")
    if len(objects) > MAX_OBJECTS:
        raise SourceError(f"objects has {len(objects)} entries; the server accepts {MAX_OBJECTS}.")
    for index, obj in enumerate(objects):
        _check_object(index, obj)

    event = {field: payload[field] for field in _FORWARDED_FIELDS if field in payload}
    event["captured_at"] = captured_at
    return event


def _check_object(index: int, obj: Any) -> None:
    """The three ways an object makes a row permanently undeliverable.

    Each of these is refused by the server's own `ObjectIn` validator, and a batch
    containing one is rejected *whole* by FastAPI before the per-item handler ever runs -
    so a single bad box spooled today poisons every batch it is drained in. The sync engine
    can isolate and discard such a row, but discarding it is losing an event. Refusing it
    here, while the producer is still on the line, is the only outcome where nothing is
    lost.
    """
    where = f"objects[{index}]"
    if not isinstance(obj, dict):
        raise SourceError(f"{where} must be an object.")
    if not isinstance(obj.get("class_name"), str) or not obj["class_name"].strip():
        raise SourceError(f"{where}.class_name is required and must be a string.")
    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise SourceError(f"{where}.confidence is required and must be a number.")
    if not 0.0 <= float(confidence) <= 1.0:
        raise SourceError(f"{where}.confidence must be between 0 and 1.")
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise SourceError(f"{where}.bbox must be [x1, y1, x2, y2] normalised to 0..1.")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in bbox):
        raise SourceError(f"{where}.bbox coordinates must be numbers.")
    if any(v < 0.0 or v > 1.0 for v in bbox):
        raise SourceError(f"{where}.bbox coordinates must be normalised to 0..1.")
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise SourceError(f"{where}.bbox must have positive area, with x1<x2 and y1<y2.")


def _build_server(
    address: tuple[str, int],
    *,
    handler: EventHandler,
    token: str | None,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 so a producer can keep the connection open across a burst; without it
        # every event costs a fresh TCP handshake on hardware where that is not free.
        protocol_version = "HTTP/1.1"
        server_version = "csense-edge-agent"
        # Suppresses the default identifier, which advertises the exact Python version to
        # anything that can reach the port.
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
            if self.path.split("?", 1)[0] == "/healthz":
                self._respond(HTTPStatus.OK, {"status": "ok"})
            else:
                self._respond(HTTPStatus.NOT_FOUND, {"error": "No such endpoint."})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/detections":
                self._respond(HTTPStatus.NOT_FOUND, {"error": "No such endpoint."})
                return
            if not self._authorised():
                self._respond(HTTPStatus.UNAUTHORIZED, {"error": "A valid bearer token is required."})
                return

            try:
                payload = self._read_json()
            except SourceError as exc:
                self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            items = payload.get("detections") if isinstance(payload, dict) else None
            if items is None:
                items = [payload]
            if not isinstance(items, list) or not items:
                self._respond(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "detections must be a non-empty list."},
                )
                return

            results: list[dict[str, Any]] = []
            for item in items:
                try:
                    event = normalise_event(item)
                except SourceError as exc:
                    # Per item, so one malformed event in a burst does not reject the
                    # burst - the same reason the server's batch endpoint reports per item.
                    results.append({"accepted": False, "error": str(exc)})
                    continue
                try:
                    outcome = self._dispatch(event)
                except Exception as exc:  # noqa: BLE001 - a producer gets an answer, always
                    logger.exception("detection_source_handler_failed")
                    results.append({"accepted": False, "error": f"The agent could not accept this event: {exc}"})
                else:
                    results.append(
                        {
                            "accepted": True,
                            "source_event_id": event["source_event_id"],
                            "outcome": outcome,
                        }
                    )

            status = HTTPStatus.ACCEPTED if any(r["accepted"] for r in results) else HTTPStatus.UNPROCESSABLE_ENTITY
            self._respond(status, {"results": results})

        # --- plumbing ---------------------------------------------------------------

        def _dispatch(self, event: Mapping[str, Any]) -> str:
            """Runs the agent's handler on its event loop and waits for the answer.

            The wait is bounded: the handler can be waiting on an HTTP request to the
            platform, and a producer thread blocked forever on a wedged uplink would leak a
            thread per event until the box ran out.
            """
            future = asyncio.run_coroutine_threadsafe(handler(event), loop)
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                future.cancel()
                raise SourceError("The agent did not accept this event in time.") from None

        def _authorised(self) -> bool:
            if not token:
                return True
            presented = self.headers.get("Authorization", "")
            scheme, _, value = presented.partition(" ")
            if scheme.lower() != "bearer":
                return False
            # Constant-time, the same discipline `deps_agent.py` applies server-side: a
            # timing-variable comparison on a shared secret is a way to recover it a byte
            # at a time from something already on the LAN.
            return hmac.compare_digest(value.strip(), token)

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise SourceError("Content-Length must be a number.") from None
            if length <= 0:
                raise SourceError("A request body is required.")
            if length > MAX_BODY_BYTES:
                raise SourceError(f"Body is {length} bytes; the limit is {MAX_BODY_BYTES}.")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SourceError(f"Body is not valid JSON: {exc}") from exc

        def _respond(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args: Any) -> None:
            # `http.server` writes to stderr by default, outside the agent's logging
            # configuration entirely, which on a device means these lines miss whatever
            # collects the rest.
            logger.debug("detection_source_request", extra={"detail": fmt % args})

    server = ThreadingHTTPServer(address, _Handler)
    # Request threads must not outlive a shutdown; without this the server waits on them,
    # and a producer holding a keep-alive connection open would stall SIGTERM.
    server.daemon_threads = True
    return server
