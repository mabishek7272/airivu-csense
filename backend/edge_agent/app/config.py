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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Reported at enrolment and on every heartbeat, so a fleet can be told which boxes are
# running which agent. In code rather than the environment: it describes the build, and a
# value an operator can set is a value that will eventually be wrong.
AGENT_VERSION = "0.1.0"

DEFAULT_STATE_DIR = "/var/lib/csense-agent"


class ConfigError(RuntimeError):
    """A setting is missing or unusable. Always fatal - see the module docstring."""


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
            spool_path=Path(env.get("CSENSE_SPOOL_PATH", f"{state_dir}/spool.sqlite3")),
            device_key_path=Path(env.get("CSENSE_DEVICE_KEY_PATH", f"{state_dir}/device.key")),
            spool_max_rows=_int(env, "CSENSE_SPOOL_MAX_ROWS", 50_000, 100, 10_000_000),
            spool_max_bytes=_int(
                env, "CSENSE_SPOOL_MAX_BYTES", 2 * 1024**3, 1024**2, 512 * 1024**3
            ),
            heartbeat_interval_seconds=_int(env, "CSENSE_HEARTBEAT_INTERVAL_SECONDS", 30, 5, 3600),
            sync_interval_seconds=_int(env, "CSENSE_SYNC_INTERVAL_SECONDS", 5, 1, 3600),
            backoff_initial_seconds=_number(env, "CSENSE_BACKOFF_INITIAL_SECONDS", 1.0, 0.1, 60.0),
            backoff_max_seconds=_number(env, "CSENSE_BACKOFF_MAX_SECONDS", 300.0, 1.0, 3600.0),
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
