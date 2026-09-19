"""The agent process: enrol once, then four loops that keep a site reporting.

Modelled on `backend/notification_worker/app/main.py`, which is this repo's established
shape for a multi-loop service, down to the deliberate asymmetry between the loops. What
that asymmetry protects is different here, and it is worth being precise about:

  **The heartbeat is the loop that must not die.** It is the only thing that tells the
  platform this box exists. Every other loop's failure is contained and logged so that the
  heartbeat keeps running — a device that has stopped delivering events but is still
  reporting `spool_depth: 40000` is a device somebody can be dispatched to. A device that
  has gone silent because its *event* loop threw is a device that looks identical to one
  whose broadband is out, and nobody learns which until they drive there. So the sync loop,
  the command loop and the detection listener are each wrapped; the heartbeat loop
  deliberately is not, and if it dies the process dies and the container restarts it.
  (`asyncio.gather` without `return_exceptions=True` propagates the first exception without
  cancelling its siblings, so an unguarded loop's exception unwinds `amain` while the others
  are still mid-flight — which is exactly the behaviour wanted for the heartbeat and exactly
  the behaviour that must not happen for the rest.)

  **Enrolment is fatal if it fails.** Unlike the notification worker's degrade-don't-refuse
  stance on optional dependencies, an agent with no credential has nothing it can do: it
  cannot deliver, cannot heartbeat, cannot be commanded. Failing loudly at start is how the
  installer standing next to the box finds out, rather than the box sitting there for a week
  looking powered on.

**Commands, and FLOW-13's "expired commands are not executed".** The server already filters
expired commands out of `/commands/pending`, but that check happens when the poll is served
and the command can expire in the interval between being handed over and being run — a
reboot command that arrives during a five-minute drain, say. So expiry is re-checked
immediately before execution, and an expired command is acked as `expired`/not-successful
rather than run. The ack matters: without it the operator's console shows `delivered`
forever with no explanation of why nothing happened.

This agent implements two commands: `ping`, and `config_push` — device-level operational
config (heartbeat interval, spool byte/row caps, backoff parameters) pushed as a signed,
versioned desired-state command
(docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-state.md, Task 4).
Everything else still acks as unsupported. That is honest rather than lazy: reboot and
model-update need artifact-verification machinery this build does not have reason to build
yet (no consumer - the edge agent stays out of model/pipeline territory, Phase 4's), and a
handler that applied something unverified would be worse than no handler at all. `config_
push`'s own "artifact verification" (FLOW-13 step 8) is `config.validate_config_push`
checking the payload against the exact bounds `AgentSettings.from_env` enforces at boot -
see that function's docstring for why reusing those bounds rather than inventing new ones
matters. Acking as `unsupported_command` (for everything still unimplemented) or `out_of_
bounds` (for a `config_push` that fails validation) means the operator sees the refusal
instead of a command sitting in `delivered` forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import platform
import signal
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import (
    AGENT_VERSION,
    AgentSettings,
    ConfigError,
    ConfigPushRejected,
    RuntimeConfig,
    validate_config_push,
)
from .crypto import load_or_create_device_key
from .logbuf import RingBufferLogHandler
from .source import LocalHttpSource
from .spool import Spool
from .sync import SyncEngine, TransportError, raise_for_batch_response
from .wireguard import WireGuardKeypair, load_or_create_keypair

logger = logging.getLogger(__name__)

# Commands this build can actually carry out. `config_push` is handled separately, ahead of
# this list - see `_run_command` - because unlike `ping` it needs `RuntimeConfig`/`Spool`/
# `SyncEngine` to act on, not just an ack to send back.
SUPPORTED_COMMANDS = ("ping",)

# A config-push command's own type, matching `tenant_api/app/api/edge.py`'s
# `CONFIG_PUSH_COMMAND_TYPE` - the two are independent constants in independent packages
# (this package cannot import `csense_shared` or anything from `tenant_api` - see this
# repo's own CLAUDE.md), so the string itself is the contract between them, not a shared
# symbol.
CONFIG_PUSH_COMMAND_TYPE = "config_push"

# How often the device asks for commands. Independent of the heartbeat interval on purpose:
# the server can widen the heartbeat cadence fleet-wide during an incident, and command
# latency should not silently widen with it.
COMMAND_POLL_SECONDS = 30.0

# What a command loop waits after a failure. Flat rather than exponential: a missed command
# poll costs latency on an operator action, never data, so there is nothing to protect by
# backing off hard - and the heartbeat is already applying real backpressure to the API.
COMMAND_RETRY_SECONDS = 60.0


class EnrolmentError(RuntimeError):
    """The agent has no usable identity. Always fatal - see the module docstring."""


# --- Identity -------------------------------------------------------------------------

def load_credential(path: Path) -> dict[str, Any] | None:
    """Reads the credential written by a previous enrolment, or None."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise EnrolmentError(
            f"The credential at {path} exists but cannot be read ({exc}). Refusing to "
            "silently re-enrol over it: that would need a fresh enrolment token, and "
            "quietly discarding a device's identity is not something to do on a guess."
        ) from exc
    if not isinstance(data, dict) or not data.get("agent_token"):
        raise EnrolmentError(f"The credential at {path} has no agent_token in it.")
    return data


def save_credential(path: Path, credential: dict[str, Any]) -> None:
    """Writes the long-lived credential, readable only by this user.

    Same reasoning as `crypto.load_or_create_device_key`'s own permission handling: this
    file is the device's whole identity, and on a box in a customer's building the other
    accounts on it are not ours. Written to a temporary file and renamed so a crash
    mid-write cannot leave a half-file that fails to parse on the next boot - which the
    loader above would then, correctly, refuse to start on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        json.dump(credential, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


async def enrol(
    client: httpx.AsyncClient, settings: AgentSettings, wireguard_keypair: WireGuardKeypair
) -> dict[str, Any]:
    """Redeems the enrolment token and returns the credential to keep.

    The serial is what the server pins the token to, so a token copied off one box cannot
    be redeemed on another. It has to be stable across reboots and reimages, which is why
    it is configuration rather than anything derived here - a generated value would make
    every reinstall look like a different device.
    """
    if not settings.enrolment_token:
        raise EnrolmentError(
            "No credential on disk and CSENSE_ENROLMENT_TOKEN is not set. The device has "
            "no way to obtain an identity; issue an enrolment token for it in the console."
        )
    if not settings.serial_number:
        raise EnrolmentError(
            "CSENSE_SERIAL_NUMBER is required to enrol - the platform pins the enrolment "
            "token to it so a token copied off one box cannot be redeemed on another."
        )

    response = await client.post(
        "/api/v1/tenant/edge/enrol",
        json={
            "token": settings.enrolment_token,
            "serial_number": settings.serial_number,
            "hardware": {"machine": platform.machine(), "processor": platform.processor()},
            "capabilities": {
                # Stated plainly rather than flattered: this build buffers and forwards, it
                # does not see. See `source.py`.
                "inference": False,
                "offline_spool": True,
                "spool_max_rows": settings.spool_max_rows,
            },
            "os_name": platform.system(),
            "os_version": platform.release(),
            "agent_version": AGENT_VERSION,
            "wireguard_public_key": wireguard_keypair.public_key_b64,
        },
    )
    if response.status_code >= 400:
        raise EnrolmentError(
            f"Enrolment was refused ({response.status_code}): {response.text[:400]}"
        )
    body = response.json()
    logger.info(
        "agent_enrolled",
        extra={"device_id": body.get("device_id"), "tenant_id": body.get("tenant_id")},
    )
    return body


# --- The platform client ----------------------------------------------------------------

def build_send_fn(client: httpx.AsyncClient):
    """The engine's `send_fn`: one HTTP POST of one batch.

    Every failure mode is mapped here rather than in `sync.py` so the delivery policy never
    has to know what an HTTP status is. `raise_for_batch_response` owns the classification
    that decides whether the engine isolates a poison row (400/413/422) or simply backs off
    (everything else, 401 and 429 included - a bad credential or a rate limit is not
    evidence about any row, and treating it as such would discard a whole spool).
    """

    async def send_fn(detections: list[dict[str, Any]]):
        try:
            response = await client.post(
                "/api/v1/tenant/ingest/detections/batch", json={"detections": detections}
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"The API could not be reached: {exc}") from exc

        raise_for_batch_response(response.status_code, response.text[:400])
        try:
            return response.json()
        except ValueError as exc:
            # A 2xx whose body is not the contract. Transport, not rejection: nothing here
            # authorises deleting anything.
            raise TransportError(f"The API's response was not JSON: {exc}") from exc

    return send_fn


# --- The loops ---------------------------------------------------------------------------

async def heartbeat_loop(
    client: httpx.AsyncClient,
    engine: SyncEngine,
    *,
    runtime_config: RuntimeConfig,
    stop: asyncio.Event,
    log_handler: RingBufferLogHandler | None = None,
) -> None:
    """Reports health, spool telemetry, and `observed_state_version`; honours whichever of
    the server's response and a `config_push` command most recently set the interval.

    Deliberately unguarded at the top level - see the module docstring. Individual request
    failures are caught (an outage is the normal case for this device, not an error), but
    anything structural is allowed to end the process so the container restarts rather than
    running on with no way to be seen.

    `log_handler` is optional so this loop stays directly testable without wiring a real
    `logging` handler into the standard library's global state for every test - see
    `logbuf.py`'s module docstring for the byte-budget reasoning behind what it contributes.

    **Reads `runtime_config.heartbeat_interval_seconds` fresh at the bottom of every
    iteration rather than caching it in a local variable at the top of the function**, which
    is what makes a `config_push` command take effect on this loop's very next wait without
    a restart: `_run_command` (running as a sibling coroutine on the same event loop, via
    `command_loop`) can mutate that attribute at any point between two iterations, and the
    next `_wait` call below picks it up because it is read, not assumed. See
    `config.RuntimeConfig`'s own docstring for why no lock is needed for that mutation to be
    safe on a single event loop.
    """
    while not stop.is_set():
        snapshot = engine.snapshot()
        health: dict[str, Any] = {
            "service": {
                "spool_bytes": snapshot["spool_bytes"],
                "uplink": "up" if snapshot["online"] else "down",
            }
        }
        if log_handler is not None:
            health["logs"] = log_handler.tail()
        try:
            response = await client.post(
                "/api/v1/tenant/edge/heartbeat",
                json={
                    "status": "ok",
                    "agent_version": AGENT_VERSION,
                    "spool_depth": snapshot["spool_depth"],
                    "spool_dropped": snapshot["spool_dropped"],
                    "health": health,
                    # First real writer of `edge_devices.observed_state_version`
                    # (migration 0042) - 0 until a config_push command has ever been
                    # successfully applied, matching that column's own default, so a device
                    # that has never received one reports "no observed state yet" rather
                    # than a value implying it converged on something nobody asked for.
                    "observed_state_version": runtime_config.observed_state_version,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Expected whenever the site is offline, which is most of what this agent is
            # for. Logged at warning, never fatal, and never escalated into a retry storm:
            # the next tick is the retry.
            logger.warning("heartbeat_failed", extra={"detail": str(exc)})
        else:
            body = response.json()
            # The platform just answered, which is the freshest possible evidence the
            # uplink is back - handing it to the engine means a device whose spool is empty
            # does not have to wait for its next event to discover the link returned.
            engine.note_link_healthy()
            returned = body.get("next_interval_seconds")
            # Once a config_push has explicitly named *this field* (`runtime_config.
            # heartbeat_interval_pinned_by_config_push`), FLOW-13's conflict policy -
            # "cloud desired state wins for configuration" - makes that versioned, acked
            # value the authoritative one. This ephemeral, unversioned advisory must not
            # silently overwrite it on the very next heartbeat, which is exactly what would
            # happen against this deployment's own `/heartbeat` route: it always returns
            # the same constant `HEARTBEAT_INTERVAL_SECONDS`, so without this guard a
            # device would revert to 30 seconds one heartbeat after a real config-push to,
            # say, 5 - acknowledging the command while silently discarding its effect.
            #
            # Deliberately gated on the flag, not on `observed_state_version != 0`: that
            # counter is bumped by *any* applied config-push, including one that only
            # changes `spool_max_rows` or a backoff parameter and never mentions the
            # interval at all. Gating on the version alone would permanently silence this
            # advisory - the mechanism `edge.py`'s own comment says exists "so the cadence
            # can be widened during an incident without shipping firmware" - for every
            # device that ever receives an unrelated config-push, which is not what
            # "cloud desired state wins" is supposed to mean for a field the cloud never
            # actually expressed an opinion on. Before any config-push names this field,
            # the advisory still governs - unchanged from this mechanism's original
            # behaviour.
            if (
                not runtime_config.heartbeat_interval_pinned_by_config_push
                and isinstance(returned, int)
                and 5 <= returned <= 3600
                and returned != runtime_config.heartbeat_interval_seconds
            ):
                logger.info("heartbeat_interval_changed", extra={"seconds": returned})
                runtime_config.heartbeat_interval_seconds = returned

        await _wait(stop, runtime_config.heartbeat_interval_seconds)


async def command_loop(
    client: httpx.AsyncClient,
    *,
    runtime_config: RuntimeConfig,
    spool: Spool,
    engine: SyncEngine,
    stop: asyncio.Event,
    interval_seconds: float = COMMAND_POLL_SECONDS,
) -> None:
    """Polls for signed commands, runs what it can, and acks everything it was handed."""
    while not stop.is_set():
        delay = interval_seconds
        try:
            response = await client.get("/api/v1/tenant/edge/commands/pending")
            response.raise_for_status()
            commands = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("command_poll_failed", extra={"detail": str(exc)})
            delay = COMMAND_RETRY_SECONDS
        else:
            for command in commands:
                await _run_command(client, command, runtime_config=runtime_config, spool=spool, engine=engine)
        await _wait(stop, delay)


async def _run_command(
    client: httpx.AsyncClient,
    command: dict[str, Any],
    *,
    runtime_config: RuntimeConfig | None = None,
    spool: Spool | None = None,
    engine: SyncEngine | None = None,
) -> None:
    """Executes one command, or explains why it did not.

    Expiry is re-checked here, not merely trusted from the poll: `/commands/pending`
    filters on the server's clock at the moment it answers, and a command can expire
    between being handed over and reaching this line - during a long drain, or while the
    process was restarting. FLOW-13's conflict policy says expired commands are not
    executed, and an expiry window that is only enforced at one end is not enforced. This
    check runs before any command-type dispatch, so it applies to `config_push` exactly as
    it already did to `ping` - an expired config-push is never applied, and
    `observed_state_version` does not move.

    `runtime_config`/`spool`/`engine` are keyword-only and default to `None` so every
    existing `ping`/unsupported-command test in `test_edge_sync.py` keeps working
    unchanged - only `config_push` ever reads them.
    """
    command_id = command.get("id")
    command_type = command.get("command_type")
    expires_at = _parse_time(command.get("expires_at"))
    now = dt.datetime.now(dt.UTC)

    if expires_at is not None and expires_at <= now:
        logger.warning(
            "command_expired_not_executed",
            extra={"command_id": command_id, "command_type": command_type},
        )
        await _ack(
            client, command_id, success=False, code="expired",
            summary="This command had expired by the time the device reached it; per "
                    "FLOW-13 an expired command is not executed.",
        )
        return

    if command_type == CONFIG_PUSH_COMMAND_TYPE:
        await _apply_config_push(
            client, command_id, command.get("payload") or {},
            runtime_config=runtime_config, spool=spool, engine=engine,
        )
        return

    if command_type not in SUPPORTED_COMMANDS:
        # Refused explicitly rather than ignored: an unacked command sits in `delivered`
        # on the operator's console forever, which reads as "the device is still working
        # on it" and is a lie.
        logger.info(
            "command_unsupported", extra={"command_id": command_id, "command_type": command_type}
        )
        await _ack(
            client, command_id, success=False, code="unsupported_command",
            summary=f"This agent build ({AGENT_VERSION}) does not implement "
                    f"'{command_type}'. Supported: "
                    f"{', '.join((*SUPPORTED_COMMANDS, CONFIG_PUSH_COMMAND_TYPE))}.",
        )
        return

    logger.info("command_executed", extra={"command_id": command_id, "command_type": command_type})
    await _ack(client, command_id, success=True, code="ok", summary="Agent is alive.")


async def _apply_config_push(
    client: httpx.AsyncClient,
    command_id: Any,
    payload: dict[str, Any],
    *,
    runtime_config: RuntimeConfig | None,
    spool: Spool | None,
    engine: SyncEngine | None,
) -> None:
    """Validates and applies one `config_push` command's payload - FLOW-13 step 8's
    "applies newer configuration only after artifact verification", for this narrow
    payload: `config.validate_config_push` checking it against the exact bounds `Agent
    Settings.from_env` enforces at boot (see that function's own docstring for why reusing
    those numbers, not a second copy of them, is the point).

    A rejected payload is never partially applied, coerced, or clamped - it is logged and
    acked with `result_code="out_of_bounds"`, and every one of `runtime_config`/`spool`/
    `engine` is left exactly as it was, `observed_state_version` included. Applying happens
    only after validation succeeds for every field in the payload at once.
    """
    if runtime_config is None or spool is None or engine is None:
        # Only reachable if a future caller wires the command loop without these - amain()
        # always supplies them. Refused rather than crashed: a device that cannot apply a
        # config-push is a device that should say so, not one that raises out of a loop the
        # module docstring promises stays isolated.
        logger.error("config_push_no_runtime_context", extra={"command_id": command_id})
        await _ack(
            client, command_id, success=False, code="internal_error",
            summary="This agent instance was not wired with a runtime config to apply "
                    "a config_push against.",
        )
        return

    target_version = payload.get("target_version")
    if not isinstance(target_version, int) or isinstance(target_version, bool) or target_version < 0:
        logger.warning("config_push_rejected", extra={"command_id": command_id, "detail": "missing target_version"})
        await _ack(
            client, command_id, success=False, code="out_of_bounds",
            summary="config_push payload is missing a valid non-negative target_version.",
        )
        return

    try:
        merged = validate_config_push(payload, current=runtime_config)
    except ConfigPushRejected as exc:
        logger.warning("config_push_rejected", extra={"command_id": command_id, "detail": str(exc)})
        await _ack(client, command_id, success=False, code="out_of_bounds", summary=str(exc))
        return

    # Applied in dependency order: the two objects that own their own copy of a bound
    # first, `runtime_config` - the single source of truth `heartbeat_loop` reads - last,
    # so a crash between these lines still leaves `Spool`/`SyncEngine` and `RuntimeConfig`
    # agreeing with each other rather than `RuntimeConfig` claiming a value neither object
    # actually holds yet. `set_limits`/`set_backoff` are each idempotent against a value
    # that has not changed, so calling both unconditionally (rather than only for the
    # fields this particular payload named) costs nothing.
    #
    # `spool.set_limits` runs via `asyncio.to_thread`, not called directly: it can run a
    # real multi-row SQLite DELETE-and-fsync sequence (a drastically lowered cap against a
    # spool genuinely near capacity - see that method's own docstring), and `spool.py`'s
    # module docstring is explicit that every one of its methods must be called this way
    # from async code, exactly as `sync.py` already does for every other caller. This is
    # the one genuine `await` point in this function - and the reason `runtime_config` is
    # left completely untouched until *after* it returns: a coroutine that runs while this
    # is suspended (`heartbeat_loop`, most likely) must see either the fully-old state or
    # the fully-new one, never a mix - which holds here because every `runtime_config.*`
    # assignment below happens only once this line has already completed, with no further
    # `await` between any of them.
    await asyncio.to_thread(spool.set_limits, max_rows=merged.spool_max_rows, max_bytes=merged.spool_max_bytes)
    engine.set_backoff(
        initial_seconds=merged.backoff_initial_seconds, max_seconds=merged.backoff_max_seconds
    )
    if "heartbeat_interval_seconds" in payload:
        # Distinct from "any config-push landed" - see `RuntimeConfig.heartbeat_interval_
        # pinned_by_config_push`'s own docstring for why a push that only touches, say,
        # `spool_max_rows` must not also silence the server's cadence-widening advisory.
        runtime_config.heartbeat_interval_pinned_by_config_push = True
    runtime_config.heartbeat_interval_seconds = merged.heartbeat_interval_seconds
    runtime_config.spool_max_rows = merged.spool_max_rows
    runtime_config.spool_max_bytes = merged.spool_max_bytes
    runtime_config.backoff_initial_seconds = merged.backoff_initial_seconds
    runtime_config.backoff_max_seconds = merged.backoff_max_seconds
    # The last write: once this is set, the next heartbeat reports convergence. Setting it
    # only after every other field above has actually been applied is what makes that
    # report true rather than aspirational.
    runtime_config.observed_state_version = target_version

    logger.info(
        "config_push_applied",
        extra={"command_id": command_id, "command_type": CONFIG_PUSH_COMMAND_TYPE},
    )
    await _ack(
        client, command_id, success=True, code="ok",
        summary=f"Applied config_push; now converging on desired_state_version {target_version}.",
    )


async def _ack(
    client: httpx.AsyncClient, command_id: Any, *, success: bool, code: str, summary: str
) -> None:
    try:
        response = await client.post(
            f"/api/v1/tenant/edge/commands/{command_id}/ack",
            json={"success": success, "result_code": code, "result_summary": summary},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Nothing is retried: the server will re-offer nothing (the command is already
        # `delivered`), and a device that hammered the ack endpoint through an outage would
        # add load at the worst moment. The console showing `delivered` is the visible
        # symptom, which is the honest one.
        logger.warning("command_ack_failed", extra={"command_id": command_id, "detail": str(exc)})


async def _isolated(name: str, coro) -> None:
    """Runs a loop so that its death is never the heartbeat's death.

    Directly modelled on `notification_worker/app/main.py`'s
    `_webhook_dispatch_never_takes_alerts_down_with_it`, and asymmetric for the same kind of
    reason: what remains after this loop dies is still worth having. A device that has
    stopped delivering events but is still reporting its spool depth is a device somebody
    can be sent to; one that has gone silent is indistinguishable from a cut cable.

    The loop is not restarted here. Silently respawning something that just failed in a way
    nobody anticipated hides it; the ERROR with traceback, and the spool depth climbing on
    the operator's console, are the signal.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise  # shutdown, not failure - must propagate or the process cannot stop
    except Exception:  # noqa: BLE001 - see this function's own docstring
        logger.exception(f"{name}_died_heartbeat_continues")


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleeps, but wakes immediately on SIGTERM. A device draining a backlog should not
    take its full interval to notice it has been asked to stop."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, signal_name), stop.set)


# --- Entry point --------------------------------------------------------------------------

async def amain() -> None:
    logging.basicConfig(
        level=os.environ.get("CSENSE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Attached to the root logger, before anything else can log, so every module's own
    # `logging.getLogger(__name__)` calls (`spool.py`, `sync.py`, `source.py`, `crypto.py`)
    # feed the same tail without each needing to know it exists - see `logbuf.py`'s module
    # docstring for the byte budget and the redaction this handler applies to every record
    # that reaches it.
    log_handler = RingBufferLogHandler()
    logging.getLogger().addHandler(log_handler)
    settings = AgentSettings.from_env()
    # What the process is *running* with, as opposed to `settings` (what it *booted* with,
    # frozen). `_run_command` mutates this in place when a `config_push` command is applied
    # - see `config.RuntimeConfig`'s own docstring for why that is safe without a lock.
    runtime_config = RuntimeConfig.from_settings(settings)

    key = load_or_create_device_key(settings.device_key_path)
    wireguard_keypair = load_or_create_keypair(settings.wireguard_key_path)
    # The private half never travels past this line - only wireguard_keypair.public_key_b64
    # goes anywhere near the network (see enrol()). Logged, not just written to the key
    # file, because the whole point of generating it here instead of asking the operator
    # to run `wg genkey` themselves is that they still need to see it once, to paste into
    # the `PrivateKey =` line of the config CSense's own vpn-provision renders - the file
    # at wireguard_key_path (first line) is the durable copy; this line is the convenient
    # one for the person standing at this device right now.
    logger.info(
        "wireguard_identity_ready",
        extra={
            "public_key": wireguard_keypair.public_key_b64,
            "private_key_file": str(settings.wireguard_key_path),
            "note": (
                "Private key is line 1 of private_key_file - paste it as this device's "
                "PrivateKey in the WireGuard config after running vpn-provision. It is "
                "never sent anywhere by this agent."
            ),
        },
    )
    spool = Spool(
        settings.spool_path,
        key,
        max_rows=settings.spool_max_rows,
        max_bytes=settings.spool_max_bytes,
    )
    # Logged rather than assumed: `synchronous` is a per-connection pragma, so "the code
    # sets it" and "this process is running with it" are different claims, and only the
    # second one keeps events across a power cut.
    logger.info("spool_opened", extra=spool.durability_settings() | {"depth": spool.depth()})

    client = httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=settings.request_timeout_seconds,
        verify=str(settings.ca_bundle_path) if settings.ca_bundle_path else True,
        headers={"User-Agent": f"csense-edge-agent/{AGENT_VERSION}"},
    )

    try:
        credential = load_credential(settings.credential_path)
        if credential is None:
            credential = await enrol(client, settings, wireguard_keypair)
            save_credential(settings.credential_path, credential)
        client.headers["Authorization"] = f"Bearer {credential['agent_token']}"
        logger.info("agent_identity_loaded", extra={"device_id": credential.get("device_id")})

        engine = SyncEngine(
            spool=spool,
            send_fn=build_send_fn(client),
            batch_size=settings.batch_size,
            idle_interval_seconds=settings.sync_interval_seconds,
            backoff_initial_seconds=settings.backoff_initial_seconds,
            backoff_max_seconds=settings.backoff_max_seconds,
        )
        source = LocalHttpSource(
            engine.submit,
            host=settings.listen_host,
            port=settings.listen_port,
            token=settings.source_token,
        )

        stop = asyncio.Event()
        install_signal_handlers(stop)
        await source.start()

        try:
            await asyncio.gather(
                # Unguarded on purpose: if the heartbeat loop dies the process should die
                # with it and be restarted, because a device nobody can see is worse than
                # a device that has stopped.
                heartbeat_loop(
                    client, engine,
                    runtime_config=runtime_config, stop=stop, log_handler=log_handler,
                ),
                _isolated("sync_loop", engine.run_forever(stop)),
                _isolated(
                    "command_loop",
                    command_loop(
                        client, runtime_config=runtime_config, spool=spool, engine=engine, stop=stop
                    ),
                ),
            )
        finally:
            stop.set()
            await source.aclose()
    finally:
        await client.aclose()
        spool.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except (ConfigError, EnrolmentError) as exc:
        # Fatal and deliberately terse. An edge device is the worst place in the system to
        # discover a configuration mistake lazily - nobody is watching its console, and the
        # failure would otherwise surface as silently undelivered events hours later.
        logger.error("agent_cannot_start", extra={"detail": str(exc)})
        print(f"csense-edge-agent: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
