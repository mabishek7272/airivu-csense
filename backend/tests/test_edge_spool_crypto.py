"""The edge agent's device-local spool crypto: the properties that make "encrypted spool"
mean something.

The threat model is not the same one `test_envelope.py` covers. That module defends stored
credentials against someone who reaches the *database* - a backup, a replica, a SQL read.
This one defends spooled events against someone who reaches the *device*: a box sitting in
a customer's warehouse, on a shelf an installer can reach, holding hours of unsent
detections because the site's broadband is down. Stolen disk, pulled SD card, a second
tenant's contractor with a screwdriver.

Two things follow, and both are tested here rather than asserted in a docstring:

  **The key is the device's own.** It is generated on the device at first run and protects
  only that device's spool. The platform KEK - the key that decrypts every tenant's camera
  passwords, TOTP secrets and webhook secrets - never leaves our infrastructure, and
  `test_a_different_device_key_cannot_open_the_row` is the shape of what a compromised
  device therefore does *not* get.

  **A ciphertext is bound to its row.** Without AAD, anyone able to write the spool file
  could reorder rows, or replay one row's payload under another row's id and timestamp,
  and the agent would upload it as a genuine event with someone else's identity on it.
  `test_a_row_cannot_be_relocated_to_another_row_id` closes that.

No database, no network, no `csense_shared` - the agent deliberately does not depend on it
(see the module docstring in `backend/edge_agent/app/__init__.py`), so neither does this.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
from pathlib import Path

import pytest


def _load_agent_module(name: str):
    """Loads one of the edge agent's modules by path.

    Every service under backend/ names its package `app`, so a plain `import app.crypto`
    resolves to whichever service another test module imported first. Same problem, and
    the same fix, as `ingest_harness._load_ingest_module` - simpler here only because the
    agent's modules import nothing from their own package, which is itself a property
    worth having: these are leaf modules with stdlib and `cryptography` beneath them and
    nothing else.
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "edge_agent" / "app" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"csense_edge_agent_{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crypto = _load_agent_module("crypto")
# Config lives here too rather than in a module of its own: it is a hundred lines of
# environment parsing with two rules worth pinning, and the agent has exactly one test
# module at this point. Split it out when there is a second thing to put beside it.
config = _load_agent_module("config")

EVENT = b'{"source_event_id":"cam2-0091","objects":[{"class_name":"person"}]}'


@pytest.fixture()
def key() -> bytes:
    return crypto.generate_device_key()


# --- Round trip -------------------------------------------------------------------------

def test_roundtrip(key):
    assert crypto.open_row(key, 7, crypto.seal_row(key, 7, EVENT)) == EVENT


def test_a_string_row_id_works_the_same(key):
    """SQLite hands back an integer rowid, but nothing about the scheme requires one."""
    sealed = crypto.seal_row(key, "cam2-0091", EVENT)
    assert crypto.open_row(key, "cam2-0091", sealed) == EVENT


def test_the_sealed_blob_does_not_contain_the_event(key):
    """The claim "the spool file holds no plaintext" starts here."""
    assert EVENT not in crypto.seal_row(key, 1, EVENT)


def test_the_same_event_seals_differently_every_time(key):
    """A fresh nonce per row, and not merely per key.

    Reusing a nonce under one AES-GCM key is not a slow degradation, it is a total break:
    two messages under the same nonce leak their XOR and hand over the authentication
    subkey. A spool writes thousands of rows under one key, which is exactly the workload
    that punishes any counter that could ever restart - so the nonce is random per call
    and never derived from a row id or a sequence.
    """
    a = crypto.seal_row(key, 1, EVENT)
    b = crypto.seal_row(key, 1, EVENT)

    assert a != b
    assert a[:crypto.NONCE_BYTES] != b[:crypto.NONCE_BYTES]


# --- Tampering and relocation -----------------------------------------------------------

def test_a_flipped_ciphertext_bit_is_rejected(key):
    sealed = bytearray(crypto.seal_row(key, 1, EVENT))
    sealed[-1] ^= 0x01

    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, 1, bytes(sealed))


def test_a_flipped_nonce_bit_is_rejected(key):
    sealed = bytearray(crypto.seal_row(key, 1, EVENT))
    sealed[0] ^= 0x01

    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, 1, bytes(sealed))


def test_a_row_cannot_be_relocated_to_another_row_id(key):
    """The attack the AAD exists to stop. Anyone who can write the spool file could
    otherwise move one row's payload under another row's id - replaying a detection under
    a different event's identity and timestamp, which the server would then accept as
    genuine because the agent's credential vouches for it."""
    sealed = crypto.seal_row(key, 7, EVENT)

    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, 8, sealed)


def test_a_different_device_key_cannot_open_the_row(key):
    """What a stolen device does *not* yield: another device's spool, let alone anything
    of the platform's."""
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(crypto.generate_device_key(), 1, crypto.seal_row(key, 1, EVENT))


def test_a_truncated_blob_is_rejected_rather_than_slicing_nonsense(key):
    """A blob too short to hold a nonce is a truncated write, not a ciphertext. Slicing it
    anyway would hand AESGCM a short nonce and raise something less honest."""
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, 1, crypto.seal_row(key, 1, EVENT)[:8])


def test_an_empty_blob_is_rejected(key):
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(key, 1, b"")


# --- Input limits -------------------------------------------------------------------------

def test_a_wrong_sized_key_is_refused(key):
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.seal_row(b"too short", 1, EVENT)
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.open_row(b"too short", 1, crypto.seal_row(key, 1, EVENT))


def test_an_empty_event_is_refused(key):
    """An empty spool row is a bug upstream, and spooling it hides that bug."""
    with pytest.raises(crypto.SpoolCryptoError):
        crypto.seal_row(key, 1, b"")


def test_an_oversized_event_is_refused(key):
    """A row the server would refuse can never be delivered, so it would sit in the spool
    forever, consuming eviction budget that belongs to events that can be."""
    with pytest.raises(crypto.SpoolCryptoError, match="bytes"):
        crypto.seal_row(key, 1, b"x" * (crypto.MAX_ROW_BYTES + 1))


# --- The device key on disk ---------------------------------------------------------------

def test_a_generated_key_is_the_right_size_and_not_repeated():
    assert len(crypto.generate_device_key()) == crypto.KEY_BYTES
    assert crypto.generate_device_key() != crypto.generate_device_key()


def test_the_key_is_created_on_first_run(tmp_path: Path):
    path = tmp_path / "nested" / "device.key"
    created = crypto.load_or_create_device_key(path)

    assert len(created) == crypto.KEY_BYTES
    assert path.read_bytes() == created


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_the_created_key_is_owner_only(tmp_path: Path):
    path = tmp_path / "device.key"
    crypto.load_or_create_device_key(path)

    assert path.stat().st_mode & 0o777 == 0o600


def test_an_existing_key_is_never_replaced(tmp_path: Path):
    """Losing this key makes every spooled event unreadable. A second call must load, not
    regenerate - including after a crash, a restart, or a config reload."""
    path = tmp_path / "device.key"
    first = crypto.load_or_create_device_key(path)

    assert crypto.load_or_create_device_key(path) == first


def test_a_spooled_row_survives_an_agent_restart(tmp_path: Path):
    """The property that matters operationally: the key outlives the process, so a device
    that reboots mid-outage can still drain what it spooled before the reboot."""
    path = tmp_path / "device.key"
    sealed = crypto.seal_row(crypto.load_or_create_device_key(path), 4, EVENT)

    assert crypto.open_row(crypto.load_or_create_device_key(path), 4, sealed) == EVENT


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_a_group_or_world_writable_key_is_refused(tmp_path: Path):
    """Mirrors `csense_shared.security.envelope.KeyRing.from_directory`: anyone who can
    rewrite the key can substitute one they control and then read everything sealed under
    it afterwards. On a box in a customer's building that is not a hypothetical."""
    path = tmp_path / "device.key"
    crypto.load_or_create_device_key(path)
    path.chmod(0o666)

    with pytest.raises(crypto.SpoolCryptoError, match="writable"):
        crypto.load_or_create_device_key(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_a_group_writable_key_is_refused_too(tmp_path: Path):
    """Group counts. "Only the docker group can write it" is a list of people."""
    path = tmp_path / "device.key"
    crypto.load_or_create_device_key(path)
    path.chmod(0o620)

    with pytest.raises(crypto.SpoolCryptoError, match="writable"):
        crypto.load_or_create_device_key(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_a_merely_readable_key_loads_but_warns(tmp_path: Path, caplog):
    """Deliberately not refused, matching `envelope.py`: Docker mounts compose secrets 0444
    so an unprivileged container user can read them, and refusing would make the correct
    containerised setup impossible and teach people to turn the check off. Readable is a
    warning; writable is a refusal."""
    path = tmp_path / "device.key"
    expected = crypto.load_or_create_device_key(path)
    path.chmod(0o444)

    with caplog.at_level("WARNING"):
        assert crypto.load_or_create_device_key(path) == expected

    assert "device_key_broadly_readable" in caplog.text


def test_a_truncated_key_file_is_refused_rather_than_used(tmp_path: Path):
    """A short read is a corrupted or partially-written key. Padding or hashing it into
    shape would silently produce a key that opens none of the spool it is meant to."""
    path = tmp_path / "device.key"
    path.write_bytes(b"\x01" * 16)
    if os.name == "posix":
        path.chmod(0o600)

    with pytest.raises(crypto.SpoolCryptoError, match="bytes"):
        crypto.load_or_create_device_key(path)


# --- Config -------------------------------------------------------------------------------

BASE_ENV = {"CSENSE_API_BASE_URL": "https://api.example.test/"}


def test_defaults_are_enough_to_start(tmp_path: Path):
    settings = config.AgentSettings.from_env({**BASE_ENV, "CSENSE_STATE_DIR": str(tmp_path)})

    # The trailing slash is stripped so paths can be joined without doubling it.
    assert settings.api_base_url == "https://api.example.test"
    assert settings.spool_path == tmp_path / "spool.sqlite3"
    assert settings.device_key_path == tmp_path / "device.key"
    assert settings.listen_host == "127.0.0.1"


def test_a_missing_api_url_is_fatal():
    with pytest.raises(config.ConfigError, match="CSENSE_API_BASE_URL"):
        config.AgentSettings.from_env({})


def test_a_nonsense_api_url_is_fatal():
    with pytest.raises(config.ConfigError, match="http"):
        config.AgentSettings.from_env({"CSENSE_API_BASE_URL": "rtsp://nvr.local"})


def test_an_unparseable_interval_is_fatal_rather_than_silently_defaulted():
    """A device that fell back to a default here would keep running on a cadence nobody
    chose, and nobody is watching its console to notice."""
    with pytest.raises(config.ConfigError, match="whole number"):
        config.AgentSettings.from_env({**BASE_ENV, "CSENSE_SYNC_INTERVAL_SECONDS": "often"})


def test_a_batch_larger_than_the_server_accepts_is_refused():
    """`MAX_BATCH` is 100 on the ingest endpoint; asking for more only earns a 422 on
    every single drain."""
    with pytest.raises(config.ConfigError, match="between 1 and 100"):
        config.AgentSettings.from_env({**BASE_ENV, "CSENSE_BATCH_SIZE": "500"})


def test_a_backoff_ceiling_below_its_floor_is_refused():
    with pytest.raises(config.ConfigError, match="ceiling"):
        config.AgentSettings.from_env({
            **BASE_ENV,
            "CSENSE_BACKOFF_INITIAL_SECONDS": "30",
            "CSENSE_BACKOFF_MAX_SECONDS": "5",
        })


def test_an_open_detection_listener_needs_a_token():
    """Bound past loopback, this listener is reachable by everything on the customer's LAN
    - the cameras included - and what it accepts becomes incidents and 3am phone calls."""
    with pytest.raises(config.ConfigError, match="CSENSE_SOURCE_TOKEN"):
        config.AgentSettings.from_env({**BASE_ENV, "CSENSE_LISTEN_HOST": "0.0.0.0"})


def test_an_open_listener_with_a_token_is_allowed():
    """The legitimate case: the source is another container, so loopback cannot reach it."""
    settings = config.AgentSettings.from_env({
        **BASE_ENV, "CSENSE_LISTEN_HOST": "0.0.0.0", "CSENSE_SOURCE_TOKEN": "s3cret",
    })

    assert settings.listen_host == "0.0.0.0"


def test_loopback_by_name_or_number_needs_no_token():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert config.AgentSettings.from_env(
            {**BASE_ENV, "CSENSE_LISTEN_HOST": host}
        ).source_token is None


def test_there_is_no_way_to_turn_off_certificate_verification():
    """Pinned deliberately. An agent that can be told to skip TLS verification is one bad
    environment variable away from shipping a whole site's detections to whoever controls
    the local DNS. A private CA gets a bundle path; nothing gets a `verify: false`."""
    fields = config.AgentSettings.__dataclass_fields__
    assert "ca_bundle_path" in fields
    assert not [f for f in fields if "verify" in f or "insecure" in f]
