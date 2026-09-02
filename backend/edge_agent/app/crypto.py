"""Device-local encryption for the offline spool.

A device that loses its uplink keeps running and writes events to a local SQLite spool,
sometimes for hours. That file sits on a box in a customer's building — on a shelf, in a
cupboard, on a wall — and the threat it has to survive is physical: a pulled SD card, a
lifted disk, a contractor with a screwdriver. So nothing in it is plaintext.

**This is not `csense_shared.security.envelope`, and must never become it.** Two
independent reasons:

1. **Security.** `envelope.py` seals against the *platform* KEK (`master_v1.key`). That one
   key decrypts every tenant's camera passwords, ONVIF logins, TOTP secrets and webhook
   secrets across the whole deployment. Shipping it to hardware in a customer's building —
   hardware we do not physically control, cannot reliably wipe, and will eventually get
   back from a decommissioned site or not at all — would put the platform's most valuable
   secret on the least defensible box in the system. **The device gets its own key,
   generated on the device at first run, and that key protects only that device's own
   spooled events.** A compromised device costs its own spool and nothing else.

2. **Fit.** `envelope.py`'s AAD binds `tenant_id`/`secret_id`/`purpose` — database-row
   concepts. A spool row is bound to a different fact: its own id in the spool file.

The primitive is nonetheless the same one, deliberately: AES-256-GCM, a fresh random
12-byte nonce per row, and the row's identity as additional authenticated data so a
ciphertext cannot be moved to another row. `envelope.py`'s docstring explains why each of
those matters — that reasoning applies here unchanged and is not restated.

There is no envelope (no per-row data key wrapped under a master key) and that is a
deliberate simplification: envelopes exist to make key *rotation* cheap and to limit how
much ciphertext one key covers. A spool is ephemeral — rows live minutes to hours and are
deleted on acknowledgement — so there is nothing long-lived to rotate, and the extra key
per row would buy a device with a slow CPU nothing at all.

Nothing here logs plaintext, and callers must not either.
"""
from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# AES-256, and GCM's standard 96-bit nonce - the size the mode is defined for.
KEY_BYTES = 32
NONCE_BYTES = 12

# Derived from the server's own limit rather than picked: `MAX_FRAME_BYTES` in
# `tenant_api/app/api/ingest.py` is 8 MiB of raw frame, which is ~10.7 MiB once base64'd
# into a detection body, plus the rest of the JSON. A row larger than the server will
# accept can never be delivered - it would sit in the spool forever, failing on every
# drain and consuming eviction budget that belongs to events that *can* be delivered. So
# it is refused at the point it would be written, where the caller can still say so.
MAX_ROW_BYTES = 12 * 1024 * 1024

# Bound the AAD input for the same reason: it comes from the caller and is hashed into
# every seal. No spool row id needs more than this.
MAX_ROW_ID_CHARS = 256


class SpoolCryptoError(RuntimeError):
    """Raised when a spool row cannot be sealed or opened, or the device key is unusable.

    Carries no detail about *why* an open failed, matching `EnvelopeError`: telling a
    caller whether it was the wrong key or a tampered ciphertext is the kind of oracle that
    turns a read-only flaw into a decryption one.
    """


def generate_device_key() -> bytes:
    """A new 32-byte device key from the OS CSPRNG."""
    return secrets.token_bytes(KEY_BYTES)


def load_or_create_device_key(path: str | Path) -> bytes:
    """The key protecting this device's spool, created on first run if absent.

    Created mode 0600, and refused if it is group- or world-**writable**. This mirrors
    `csense_shared.security.envelope.KeyRing.from_directory`'s handling and its reasoning:
    anyone who can rewrite the key can substitute one they control and then read everything
    sealed under it afterwards. On a device sitting in a building we do not control, that
    is a live risk rather than a theoretical one.

    It deliberately does **not** refuse a merely *readable* key, matching that module for
    the same reason: Docker mounts compose secrets 0444 so an unprivileged container user
    can read them, and refusing would make the correct containerised setup impossible and
    teach people to turn the check off. Readable warns; writable refuses.

    Windows is not checked, because POSIX mode bits are not comparable to Windows ACLs and
    approximating them badly is worse than not checking (again, `envelope.py`'s choice).

    Creation is atomic and never clobbers: the key is written to a temporary file in the
    same directory, flushed to disk, and then *linked* into place. `os.link` fails if the
    destination exists, so a second process racing to first-run cannot overwrite the key
    the first one just created - and a crash between the write and the link leaves a
    stray temp file rather than a half-written key. That matters more here than almost
    anywhere: losing or corrupting this key does not lose a credential that can be
    reissued, it makes every event already in the spool permanently unreadable.
    """
    path = Path(path)
    if path.exists():
        return _read_existing_key(path)

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = generate_device_key()

    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".device-key-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            # Another process created the key between the check above and now. Theirs
            # wins - the alternative is two processes each believing in a different key,
            # and half the spool being unreadable by whichever one restarts.
            return _read_existing_key(path)
    finally:
        # Best effort: the link (if it succeeded) already made the content durable under
        # the real name, and a leftover temp file is untidy, not dangerous.
        try:
            os.unlink(temp_name)
        except OSError:  # pragma: no cover - only on a filesystem that lost the temp file
            pass

    logger.info("device_spool_key_created", extra={"path": str(path)})
    return key


def _read_existing_key(path: Path) -> bytes:
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o022:
            raise SpoolCryptoError(
                f"Device key '{path}' is mode {mode:o} and is writable by group or others. "
                "Anyone who can rewrite it can substitute a key they control and read "
                "every event sealed under it afterwards (chmod 600)."
            )
        if mode & 0o044:
            logger.warning(
                "device_key_broadly_readable", extra={"path": str(path), "mode": f"{mode:o}"}
            )

    raw = path.read_bytes()
    if len(raw) != KEY_BYTES:
        # No stripping, no padding, no hashing into shape: a short read means a corrupt or
        # partially-written file, and coercing it would produce a key that silently opens
        # none of the spool it is meant to.
        raise SpoolCryptoError(
            f"Device key '{path}' is {len(raw)} bytes; {KEY_BYTES} required. It is "
            "corrupt or was only partly written."
        )
    return raw


def _aad(row_id: int | str) -> bytes:
    """What a ciphertext is bound to: the spool row it belongs to.

    GCM authenticates this without encrypting it, so opening a row under any other id
    fails. Without it, anyone able to write the spool file could move one row's payload
    under another row's id - replaying a detection with a different event's identity and
    timestamp, which the server would then accept as genuine because the device's own
    credential vouches for the upload.
    """
    text = str(row_id)
    if not text or len(text) > MAX_ROW_ID_CHARS:
        raise SpoolCryptoError("A spool row id must be non-empty and short.")
    return b"csense-spool-v1|" + text.encode()


def _checked_key(key: bytes) -> AESGCM:
    if len(key) != KEY_BYTES:
        raise SpoolCryptoError(f"Device key is {len(key)} bytes; {KEY_BYTES} required.")
    return AESGCM(key)


def seal_row(key: bytes, row_id: int | str, plaintext: bytes) -> bytes:
    """Encrypts one spool row. Returns `nonce || ciphertext+tag` as a single blob.

    One blob rather than separate columns because a spool row's payload is one SQLite
    BLOB, and splitting the nonce out would only create a way to store a row whose two
    halves do not belong together.

    The nonce is random per call, never derived from `row_id` or a sequence. A spool writes
    thousands of rows under one key, and any counter that could restart - after a crash, a
    reimage, a restored backup of the spool file - would repeat a nonce. Nonce reuse under
    AES-GCM is not a slow degradation: two messages under the same nonce leak their XOR and
    the authentication subkey with them.
    """
    if not plaintext:
        raise SpoolCryptoError("Refusing to seal an empty spool row.")
    if len(plaintext) > MAX_ROW_BYTES:
        raise SpoolCryptoError(
            f"Spool row is {len(plaintext)} bytes; the limit is {MAX_ROW_BYTES}. A row the "
            "server would refuse can never be delivered."
        )

    cipher = _checked_key(key)
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + cipher.encrypt(nonce, plaintext, _aad(row_id))


def open_row(key: bytes, row_id: int | str, blob: bytes) -> bytes:
    """Decrypts one spool row, or refuses.

    Returns bytes rather than str deliberately, the same nudge `envelope.decrypt` makes:
    the value is an event payload to be forwarded, not a string to format into a log line.
    """
    cipher = _checked_key(key)
    if len(blob) <= NONCE_BYTES:
        # Too short to hold a nonce and a tag: a truncated write, not a ciphertext.
        # Slicing it anyway would hand AESGCM a short nonce and raise something less honest.
        raise SpoolCryptoError("Spool row is truncated.")

    try:
        return cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _aad(row_id))
    except InvalidTag as exc:
        raise SpoolCryptoError("Spool row failed authentication (key, row, or content).") from exc
