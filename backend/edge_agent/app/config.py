"""The agent's settings, read from the environment.

Deliberately **not** `csense_shared.config.Settings`. That class is a pydantic-settings
model describing a server: Postgres DSNs, Redis, MinIO, JWT keypairs, master key
directories — none of which exist on an edge device, and importing it would drag the whole
server dependency tree onto a Pi (see this package's `__init__.py`). Ninety lines of
stdlib is the cheaper end of that trade.

Every name is prefixed `CSENSE_`. The agent runs on hardware we do not own, alongside
whatever else the customer's integrator installed, and a bare `SPOOL_PATH` or `API_URL` in
that environment is a collision waiting to happen.

Settings are validated once, at start, and a bad one is fatal. An edge device is the worst
place in the system to discover a configuration mistake lazily: nobody is watching its
console, and the failure would otherwise surface as silently unsent events hours later.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Reported at enrolment and on every heartbeat, so a fleet can be told which boxes are
# running which agent. In code rather than the environment: it describes the build, and a
# value an operator can set is a value that will eventually be wrong.
AGENT_VERSION = "0.1.0"

DEFAULT_STATE_DIR = "/var/lib/csense-agent"


class ConfigError(RuntimeError):
    """A setting is missing or unusable. Always fatal - see the module docstring."""


# --- Bounds shared between boot-time env parsing and a runtime config-push command ------
#
# docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-state.md, Task 4.
# `AgentSettings.from_env` already enforces a (low, high) range on each of these five
# fields when the process starts (`_int`/`_number` below). A `config_push` command is the
# same kind of assertion arriving over the wire instead of the environment, and the task's
# own instruction is explicit: "reject a command whose values fail the same bounds
# config.py already enforces at its own startup validation ... reuse those bounds checks,
# don't duplicate them with different numbers." So these five constants are the *one* place
# each range is written down; `from_env` passes them to `_int`/`_number` positionally and
# `validate_config_push` below reads the same tuples - a number changed here changes what
# both a boot-time env var and a live config-push command will accept, together.
HEARTBEAT_INTERVAL_BOUNDS = (5, 3600)
SPOOL_MAX_ROWS_BOUNDS = (100, 10_000_000)
SPOOL_MAX_BYTES_BOUNDS = (1024**2, 512 * 1024**3)
BACKOFF_INITIAL_BOUNDS = (0.1, 60.0)
BACKOFF_MAX_BOUNDS = (1.0, 3600.0)


@dataclass(frozen=True)
class AgentSettings:
    """Everything the agent needs to know, resolved and checked."""

    # --- The platform ---
    api_base_url: str
    request_timeout_seconds: float
    # A private or internal CA, if the deployment terminates TLS with one. There is
    # deliberately no "verify: false" switch: an agent that can be told to skip
    # certificate verification is one bad env var away from shipping every detection at a
    # site to whoever holds the local DNS, and the tunnel modes this platform recommends
    # (WireGuard, cloud relay) already give a private CA somewhere to live.
    ca_bundle_path: Path | None

    # --- Identity ---
    # Single-use and short-lived; needed only for the one boot that redeems it. An
    # environment variable is the right shape for exactly that: it is meant to travel (USB
    # stick, an installer reading it over the phone) and to stop working afterwards.
    enrolment_token: str | None
    # Pinned to this device at enrolment, so a token copied off one box cannot be redeemed
    # on another.
    serial_number: str | None
    # The long-lived agent credential, written here once enrolment issues it. A file, not
    # an environment variable, for the same reasons `envelope.py` keeps the master key in
    # one: a file can be mode-restricted, stays out of `docker inspect` and out of any
    # child process's environment, and can be replaced without a rebuild.
    credential_path: Path
    # This device's WireGuard identity (see wireguard.py) - generated on first run,
    # persisted here, and never transmitted as anything but its public half.
    wireguard_key_path: Path

    # --- The spool ---
    spool_path: Path
    device_key_path: Path
    # Two bounds, because either one alone lies. Rows alone would let a spool of
    # frame-carrying events fill a 32 GB SD card; bytes alone would let a flood of tiny
    # events cost unbounded SQLite overhead. Whichever is hit first evicts.
    spool_max_rows: int
    spool_max_bytes: int

    # --- Cadence ---
    # A starting point only: the server returns `next_interval_seconds` on every heartbeat
    # so the fleet's cadence can be widened during an incident without shipping firmware.
    heartbeat_interval_seconds: int
    sync_interval_seconds: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    # The server caps a batch at 100 (`MAX_BATCH`); asking for more only earns a 422.
    batch_size: int

    # --- The local detection source ---
    # Loopback by default and that is a security choice, not a placeholder: this listener
    # accepts events that become detections, incidents and 3am phone calls. Bound to
    # 0.0.0.0 without a token it would be an unauthenticated event injector reachable by
    # anything on the customer's LAN - including the cameras themselves, which are the
    # least trustworthy devices on that network.
    listen_host: str
    listen_port: int
    source_token: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentSettings:
        env = os.environ if env is None else env
        state_dir = env.get("CSENSE_STATE_DIR", DEFAULT_STATE_DIR)

        listen_host = env.get("CSENSE_LISTEN_HOST", "127.0.0.1").strip()
        source_token = _optional(env, "CSENSE_SOURCE_TOKEN")
        if not _is_loopback(listen_host) and not source_token:
            raise ConfigError(
                f"CSENSE_LISTEN_HOST is '{listen_host}', which is reachable from outside "
                "this device, but CSENSE_SOURCE_TOKEN is not set. An open detection "
                "listener on a site LAN lets anything on that network manufacture "
                "incidents. Bind to 127.0.0.1, or set a token the source must present."
            )

        settings = cls(
            api_base_url=_url(env, "CSENSE_API_BASE_URL"),
            request_timeout_seconds=_number(env, "CSENSE_REQUEST_TIMEOUT_SECONDS", 30.0, 1.0, 300.0),
            ca_bundle_path=_optional_path(env, "CSENSE_CA_BUNDLE_PATH"),
            enrolment_token=_optional(env, "CSENSE_ENROLMENT_TOKEN"),
            serial_number=_optional(env, "CSENSE_SERIAL_NUMBER"),
            credential_path=Path(env.get("CSENSE_CREDENTIAL_PATH", f"{state_dir}/agent.credential")),
            wireguard_key_path=Path(env.get("CSENSE_WIREGUARD_KEY_PATH", f"{state_dir}/wireguard.key")),
            spool_path=Path(env.get("CSENSE_SPOOL_PATH", f"{state_dir}/spool.sqlite3")),
            device_key_path=Path(env.get("CSENSE_DEVICE_KEY_PATH", f"{state_dir}/device.key")),
            spool_max_rows=_int(env, "CSENSE_SPOOL_MAX_ROWS", 50_000, *SPOOL_MAX_ROWS_BOUNDS),
            spool_max_bytes=_int(env, "CSENSE_SPOOL_MAX_BYTES", 2 * 1024**3, *SPOOL_MAX_BYTES_BOUNDS),
            heartbeat_interval_seconds=_int(
                env, "CSENSE_HEARTBEAT_INTERVAL_SECONDS", 30, *HEARTBEAT_INTERVAL_BOUNDS
            ),
            sync_interval_seconds=_int(env, "CSENSE_SYNC_INTERVAL_SECONDS", 5, 1, 3600),
            backoff_initial_seconds=_number(
                env, "CSENSE_BACKOFF_INITIAL_SECONDS", 1.0, *BACKOFF_INITIAL_BOUNDS
            ),
            backoff_max_seconds=_number(env, "CSENSE_BACKOFF_MAX_SECONDS", 300.0, *BACKOFF_MAX_BOUNDS),
            batch_size=_int(env, "CSENSE_BATCH_SIZE", 100, 1, 100),
            listen_host=listen_host,
            listen_port=_int(env, "CSENSE_LISTEN_PORT", 8099, 1, 65535),
            source_token=source_token,
        )

        if settings.backoff_max_seconds < settings.backoff_initial_seconds:
            raise ConfigError(
                "CSENSE_BACKOFF_MAX_SECONDS is below CSENSE_BACKOFF_INITIAL_SECONDS, so "
                "the backoff ceiling would cut the first retry short."
            )
        if settings.ca_bundle_path and not settings.ca_bundle_path.is_file():
            raise ConfigError(f"CSENSE_CA_BUNDLE_PATH '{settings.ca_bundle_path}' is not a file.")
        return settings


def _optional(env, name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _optional_path(env, name: str) -> Path | None:
    value = _optional(env, name)
    return Path(value) if value else None


def _url(env, name: str) -> str:
    value = _optional(env, name)
    if not value:
        raise ConfigError(f"{name} is required - the agent has nowhere to report to.")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{name} must be an http(s) URL, not '{value}'.")
    return value.rstrip("/")


def _int(env, name: str, default: int, low: int, high: int) -> int:
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, not '{raw}'.") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} is {value}; it must be between {low} and {high}.")
    return value


def _number(env, name: str, default: float, low: float, high: float) -> float:
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, not '{raw}'.") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} is {value}; it must be between {low} and {high}.")
    return value


# --- Runtime-mutable config: what a `config_push` command may change while running -------
#
# `AgentSettings` stays frozen and env-derived - it answers "what did this process boot
# with". `RuntimeConfig` answers "what is it running with right now", and the two are
# allowed to diverge the moment a config-push command is applied - that divergence *is*
# `edge_devices.desired_state_version` vs. `observed_state_version` (migration 0042),
# visible over the wire rather than only in this process's memory.
@dataclass
class RuntimeConfig:
    """The five fields `main.py` actually reads on every loop iteration rather than once
    at startup - see that module for where each is consumed (`heartbeat_loop` reads
    `heartbeat_interval_seconds` fresh before every wait; `Spool.set_limits` and
    `SyncEngine.set_backoff` are called with the rest whenever a config-push changes them).

    A plain dataclass, not frozen, and mutated by plain attribute assignment rather than a
    lock. That is safe here for a reason `logbuf.py`'s `RingBufferLogHandler` (which does
    need a lock) does not share: every reader and writer of this object runs as a coroutine
    on `main.py`'s single asyncio event loop, and asyncio only ever switches between
    coroutines at an `await` - never in the middle of one Python-level attribute read or
    write. `logbuf.py` needs a lock because the standard `logging` module can call its
    handler from a thread this package does not control; nothing here is ever touched from
    a thread.
    """

    heartbeat_interval_seconds: int
    spool_max_rows: int
    spool_max_bytes: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    # `edge_devices.desired_state_version` this config currently reflects. 0 until the
    # first config-push command is ever applied, matching that column's own default
    # (migration 0042) - an agent that has never received one has nothing to have
    # converged on yet.
    observed_state_version: int = 0
    # Whether a config-push has ever named `heartbeat_interval_seconds` specifically -
    # deliberately *not* inferred from `observed_state_version != 0`. A device can apply a
    # config-push that only touches, say, `spool_max_rows`; that still bumps `observed_
    # state_version`, but it says nothing about the cloud having an opinion on this
    # device's heartbeat cadence. `main.py`'s `heartbeat_loop` reads this flag, not the
    # version counter, to decide whether the server's own `next_interval_seconds` advisory
    # (used for fleet-wide incident-driven widening, per `edge.py`'s own comment) is still
    # allowed to move the interval - collapsing that decision onto "any config-push ever
    # landed" would permanently silence the advisory for every device that ever received a
    # push about anything else, defeating the one thing that field exists for.
    heartbeat_interval_pinned_by_config_push: bool = False

    @classmethod
    def from_settings(cls, settings: AgentSettings) -> RuntimeConfig:
        return cls(
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
            spool_max_rows=settings.spool_max_rows,
            spool_max_bytes=settings.spool_max_bytes,
            backoff_initial_seconds=settings.backoff_initial_seconds,
            backoff_max_seconds=settings.backoff_max_seconds,
        )


class ConfigPushRejected(RuntimeError):
    """A `config_push` command's payload failed the same bounds `from_env` enforces at
    startup, or named none of the fields this build can change. Raised rather than
    returned, so a caller cannot forget to check a return value and apply a value that was
    never actually validated - `main.py`'s `_run_command` is the one place this is caught,
    and it turns this into a visible, logged ack rejection (never a silently-ignored or
    clamped-and-applied command - see this repo's Task 4 write-up)."""


# name -> (python type, (low, high)). The *only* five fields a config-push command may
# name - deliberately not every `AgentSettings` field (`api_base_url`, `listen_port`, the
# credential paths, ...): this project's own scope decision
# (docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-desired-state.md) is
# device-level *operational* config only, matching what CSENSE_* env vars already expose as
# runtime-tunable at boot. Pushing, say, a new `api_base_url` over a channel authenticated
# *against* that same URL is a different, much harder problem this task does not attempt.
CONFIG_PUSH_FIELDS: dict[str, tuple[type, tuple[float, float]]] = {
    "heartbeat_interval_seconds": (int, HEARTBEAT_INTERVAL_BOUNDS),
    "spool_max_rows": (int, SPOOL_MAX_ROWS_BOUNDS),
    "spool_max_bytes": (int, SPOOL_MAX_BYTES_BOUNDS),
    "backoff_initial_seconds": (float, BACKOFF_INITIAL_BOUNDS),
    "backoff_max_seconds": (float, BACKOFF_MAX_BOUNDS),
}


def validate_config_push(payload: dict[str, Any], *, current: RuntimeConfig) -> RuntimeConfig:
    """Checks a config-push command's payload against the same bounds `from_env` enforces
    at startup, and returns what the fully-merged runtime config *would* be if applied -
    the caller (`main.py`) still owns deciding when and how to mutate the live `Spool`/
    `SyncEngine`/heartbeat cadence, this function only decides whether the payload is safe
    to apply at all.

    Unrecognised keys in the payload (anything not in `CONFIG_PUSH_FIELDS`, including the
    command's own `target_version` bookkeeping field) are ignored rather than rejected -
    this is an envelope shared with server-side metadata, not a strict schema the agent
    owns. A payload naming *no* recognised field is rejected, though: a config-push command
    that changes nothing this build understands is a bug worth surfacing as a rejection,
    not a silent no-op ack of `success`.

    Raises `ConfigPushRejected` naming exactly what failed - never coerces, clamps, or
    partially applies. The one cross-field check `from_env` also makes (backoff ceiling
    below floor) is re-checked here against the *merged* result, not just the two fields in
    isolation - a payload that only lowers `backoff_max_seconds` below the device's
    existing `backoff_initial_seconds` must be caught exactly as if both had arrived in the
    same push.
    """
    updates: dict[str, int | float] = {}
    for name, (kind, (low, high)) in CONFIG_PUSH_FIELDS.items():
        if name not in payload:
            continue
        raw = payload[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigPushRejected(f"{name} must be a number, not {raw!r}.")
        value = kind(raw)
        if not low <= value <= high:
            raise ConfigPushRejected(f"{name} is {value}; it must be between {low} and {high}.")
        updates[name] = value

    if not updates:
        raise ConfigPushRejected(
            "This config-push payload named none of the fields this agent build can "
            f"change ({', '.join(sorted(CONFIG_PUSH_FIELDS))})."
        )

    merged = replace(current, **updates)
    if merged.backoff_max_seconds < merged.backoff_initial_seconds:
        raise ConfigPushRejected(
            "backoff_max_seconds would end up below backoff_initial_seconds, so the "
            "backoff ceiling would cut the first retry short."
        )
    return merged


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we cannot classify without resolving it, and resolution at config
        # time would make the check depend on whatever DNS the site happens to serve.
        # Treated as not-loopback, which only ever asks for a token that was not needed.
        return False
