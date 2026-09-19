"""This device's own WireGuard identity.

Separate from `crypto.py`'s device key on purpose: that key encrypts the spool and never
leaves this process. A WireGuard keypair is the opposite shape - the *public* half is
meant to travel (it rides in the enrolment call to CSense, which embeds it in the shared
server's own peer config), while the *private* half must never leave this device either,
matching what `_render_client_config` on the server already promises operators ("CSense
neither sees nor stores it"). Two different keys, two different rules, so kept in two
different files rather than one growing to mean both things.

Curve25519, the exact primitive WireGuard itself uses - not a proxy for it. A WireGuard
public key is just that keypair's 32-byte public value, base64-encoded, which is why the
platform's own `EnrolIn.wireguard_public_key` pattern (`^[A-Za-z0-9+/]{43}=$`) is exactly
what `X25519PublicKey`'s raw bytes look like once base64-encoded - no format translation
needed between what this module produces and what the server expects.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WireGuardKeypair:
    private_key_b64: str
    public_key_b64: str


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def generate_keypair() -> WireGuardKeypair:
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()
    from cryptography.hazmat.primitives import serialization

    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return WireGuardKeypair(private_key_b64=_b64(private_raw), public_key_b64=_b64(public_raw))


def load_or_create_keypair(path: str | Path) -> WireGuardKeypair:
    """The keypair this device presents at enrolment, created on first run if absent.

    Stored as `<private_key_b64>\\n<public_key_b64>\\n` - two lines, not JSON, so the
    private line alone is exactly what an operator pastes as `PrivateKey =` in a real
    `wg-quick` config with nothing to strip. Same atomic-create-once discipline as
    `crypto.load_or_create_device_key`: written to a temp file and linked into place, so a
    second process racing to first-run cannot overwrite what the first one just created,
    and a crash mid-write cannot leave a half-written key that a later boot mistakes for
    real. Mode 0600 for the same reason as that key - this is device identity, not
    something the other accounts on a box we do not control should be able to read.
    """
    path = Path(path)
    if path.exists():
        return _read_existing(path)

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    keypair = generate_keypair()
    content = f"{keypair.private_key_b64}\n{keypair.public_key_b64}\n"

    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".wireguard-key-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            # Another process created it first - theirs wins, same reasoning as the
            # device key: two processes believing in two different keypairs is worse
            # than either one alone.
            return _read_existing(path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:  # pragma: no cover - only on a filesystem that lost the temp file
            pass

    logger.info("wireguard_keypair_created", extra={"path": str(path)})
    return keypair


def _read_existing(path: Path) -> WireGuardKeypair:
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o022:
            raise RuntimeError(
                f"WireGuard key file '{path}' is mode {mode:o} and is writable by group "
                "or others. Anyone who can rewrite it can substitute a keypair they "
                "control (chmod 600)."
            )

    lines = path.read_text().splitlines()
    if len(lines) < 2 or not lines[0] or not lines[1]:
        raise RuntimeError(
            f"WireGuard key file '{path}' does not contain two non-empty lines "
            "(private key, public key). It is corrupt or was only partly written."
        )
    return WireGuardKeypair(private_key_b64=lines[0], public_key_b64=lines[1])
