"""Envelope encryption for credentials at rest (TRD-SEC-004).

Camera RTSP passwords, ONVIF logins and similar live in the database. A database backup,
a replica, or a SQL-injection read must not hand an attacker a working camera password -
so the plaintext is never stored, and the key that would decrypt it is not in the database
at all.

**Why envelope rather than a single key.** Each secret gets its own random data key (DEK),
and only that DEK is encrypted with the master key (KEK). Three things follow:

  - The KEK never touches a large volume of ciphertext, which is what key-reuse limits are
    actually about. AES-GCM is safe for far fewer messages under one key than people
    assume.
  - Rotating the KEK rewraps a small DEK per row rather than decrypting and re-encrypting
    every secret.
  - A leaked DEK exposes exactly one credential.

**Every ciphertext is bound to its row.** The tenant id, secret id and purpose are passed
as AES-GCM additional authenticated data. They are not encrypted - they are *authenticated*,
so decryption fails if any of them differs from what was used to encrypt. Without this,
anyone able to write to the database could copy tenant A's encrypted camera password into
tenant B's camera row and the system would decrypt it happily. That is a real
privilege-escalation path in a multi-tenant system, and the AAD closes it.

**The KEK comes from a file, not the database and not an environment variable.** Files can
be mounted read-only, kept out of `docker inspect`, and swapped without a rebuild. The
`kek_id` recorded alongside each secret is what makes rotation possible: old rows stay
readable under the old key while new rows use the new one.

Nothing here logs plaintext, and callers must not either. `decrypt` returns bytes rather
than str deliberately - it is a nudge that the value is not a display string.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# AES-256. A 32-byte key, and GCM's standard 96-bit nonce - the size the mode is defined
# for, and the only one that avoids an extra hashing step during nonce derivation.
KEY_BYTES = 32
NONCE_BYTES = 12

# A stored credential should never approach this. The cap exists so a malformed or hostile
# value cannot be used to allocate unbounded memory during decryption.
MAX_PLAINTEXT_BYTES = 8 * 1024


class EnvelopeError(RuntimeError):
    """Raised when a secret cannot be sealed or opened.

    Deliberately carries no detail about *why* beyond a short reason: distinguishing "wrong
    key" from "tampered ciphertext" for a caller is exactly the kind of oracle that turns a
    read-only flaw into a decryption one.
    """


@dataclass(frozen=True)
class SealedSecret:
    """An encrypted value, ready to be stored. No field here is sensitive on its own."""

    kek_id: str
    wrapped_dek: bytes      # the DEK, encrypted under the KEK
    dek_nonce: bytes
    ciphertext: bytes       # the secret, encrypted under the DEK
    ciphertext_nonce: bytes

    def as_row(self) -> dict:
        """Base64 for columns, so the values survive text encoding and log redaction."""
        return {
            "kek_id": self.kek_id,
            "wrapped_dek": base64.b64encode(self.wrapped_dek).decode(),
            "dek_nonce": base64.b64encode(self.dek_nonce).decode(),
            "ciphertext": base64.b64encode(self.ciphertext).decode(),
            "ciphertext_nonce": base64.b64encode(self.ciphertext_nonce).decode(),
        }

    @classmethod
    def from_row(cls, row: dict) -> SealedSecret:
        try:
            return cls(
                kek_id=row["kek_id"],
                wrapped_dek=base64.b64decode(row["wrapped_dek"]),
                dek_nonce=base64.b64decode(row["dek_nonce"]),
                ciphertext=base64.b64decode(row["ciphertext"]),
                ciphertext_nonce=base64.b64decode(row["ciphertext_nonce"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EnvelopeError("Stored secret is malformed.") from exc


class KeyRing:
    """The master keys, loaded from disk once.

    Holds every KEK the deployment knows about, not just the current one, so secrets
    written under a previous key stay readable through a rotation. `active_id` is the key
    new secrets are written under.
    """

    def __init__(self, keys: dict[str, bytes], active_id: str) -> None:
        if not keys:
            raise EnvelopeError("No master keys were loaded.")
        if active_id not in keys:
            raise EnvelopeError(f"Active key '{active_id}' is not among the loaded keys.")
        for key_id, key in keys.items():
            if len(key) != KEY_BYTES:
                raise EnvelopeError(
                    f"Master key '{key_id}' is {len(key)} bytes; {KEY_BYTES} required."
                )
        self._keys = keys
        self.active_id = active_id

    def get(self, key_id: str) -> bytes:
        key = self._keys.get(key_id)
        if key is None:
            # A secret encrypted under a key this deployment does not have. Naming the id
            # is safe and is the only way an operator can work out which key is missing.
            raise EnvelopeError(f"No master key with id '{key_id}' is available.")
        return key

    @property
    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    @classmethod
    def from_directory(
        cls,
        directory: str | Path,
        active_id: str | None = None,
        *,
        strict_permissions: bool = True,
    ) -> KeyRing:
        """Loads every `*.key` file in a directory.

        The filename stem is the key id, so `master_v1.key` holds the key referenced as
        `master_v1`. Each file must contain exactly 32 raw bytes or their base64 form.

        Permissions are checked on POSIX, and the two cases differ:

          *Writable* by group or others is the serious one. Anyone who can rewrite the
          master key can substitute one they control, then wait for secrets to be
          re-sealed under it. Refused when `strict_permissions` is set.

          *Readable* by group or others is only warned about. Docker mounts compose
          secrets mode 0444 so an unprivileged container user can read them; refusing
          would make the correct containerised setup impossible and teach people to turn
          the check off.

        `strict_permissions=False` exists for one specific reason: Docker Desktop on
        Windows cannot represent POSIX modes on a bind mount and reports every file as
        0777, so the writable check fires on every local developer machine while telling
        you nothing. Callers pass False only for local development - see
        `keyring_from_settings`, which keys it off the environment so a production
        deployment can never quietly skip it.

        Windows ACLs are not comparable to POSIX mode bits, so the check does not run
        there rather than being approximated badly.
        """
        path = Path(directory)
        if not path.is_dir():
            raise EnvelopeError(f"Master key directory '{directory}' does not exist.")

        keys: dict[str, bytes] = {}
        for entry in sorted(path.glob("*.key")):
            if os.name == "posix":
                mode = entry.stat().st_mode & 0o777
                if mode & 0o022:
                    if strict_permissions:
                        raise EnvelopeError(
                            f"Master key '{entry.name}' is mode {mode:o} and is writable "
                            "by group or others. Anyone who can rewrite it can substitute "
                            "a key they control (chmod 400)."
                        )
                    logger.warning(
                        "master_key_writable_permissions_not_enforced",
                        extra={"key": entry.name, "mode": f"{mode:o}"},
                    )
                elif mode & 0o044:
                    logger.warning(
                        "master_key_broadly_readable",
                        extra={"key": entry.name, "mode": f"{mode:o}"},
                    )
            keys[entry.stem] = _read_key(entry)

        if not keys:
            raise EnvelopeError(f"No *.key files found in '{directory}'.")

        # Default to the highest id, so `v2.key` takes over from `v1.key` by being added.
        return cls(keys, active_id or sorted(keys)[-1])


def _read_key(path: Path) -> bytes:
    """Reads a key file holding either 32 raw bytes or their base64 form.

    The raw case is checked *before* stripping, and that ordering is the whole point.
    Random key material contains whatever bytes it contains, including 0x20, 0x09 and
    0x0a - all of which `bytes.strip()` removes. Stripping first turns roughly one
    generated key in twenty-two into an unloadable 31-byte file, and the failure looks
    like a corrupt key rather than a bug here.

    Trailing whitespace only matters for the base64 form, where an editor or `echo` may
    have added a newline, so the strip happens on that path alone.
    """
    raw = path.read_bytes()
    if len(raw) == KEY_BYTES:
        return raw

    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError(
            f"Master key '{path.name}' is neither {KEY_BYTES} raw bytes nor valid base64."
        ) from exc

    if len(decoded) != KEY_BYTES:
        raise EnvelopeError(
            f"Master key '{path.name}' decodes to {len(decoded)} bytes; "
            f"{KEY_BYTES} required."
        )
    return decoded


def keyring_from_settings(settings) -> KeyRing:
    """The deployment's keyring, with permission strictness tied to the environment.

    The permission check is relaxed *only* for `ENVIRONMENT=local`, because Docker Desktop
    on Windows reports every bind-mounted file as 0777 and the check would otherwise fire
    on every developer machine. Any other environment enforces it, so a production
    deployment cannot end up skipping it by inheriting a developer's configuration.
    """
    return KeyRing.from_directory(
        settings.master_key_dir,
        settings.master_key_active_id or None,
        strict_permissions=settings.environment != "local",
    )


def generate_master_key() -> bytes:
    """A new 32-byte master key, for `openssl rand`-free key creation in setup scripts."""
    return secrets.token_bytes(KEY_BYTES)


def _aad(tenant_id: UUID | None, secret_id: UUID, purpose: str) -> bytes:
    """The context a ciphertext is bound to.

    Including the tenant means a row cannot be moved between tenants; including the secret
    id means it cannot be moved between rows; including the purpose means a stored RTSP
    password cannot be presented where an API token is expected. All three are checked by
    GCM at decryption time.

    A platform-owned secret has no tenant, written as an empty field rather than omitted,
    so the structure of the AAD never varies.
    """
    return b"|".join(
        [
            b"csense-envelope-v1",
            str(tenant_id).encode() if tenant_id else b"",
            str(secret_id).encode(),
            purpose.encode(),
        ]
    )


def seal(
    keyring: KeyRing,
    plaintext: bytes | str,
    *,
    tenant_id: UUID | None,
    secret_id: UUID,
    purpose: str,
) -> SealedSecret:
    """Encrypts a credential for storage."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode()
    if not plaintext:
        raise EnvelopeError("Refusing to seal an empty value.")
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise EnvelopeError(
            f"Value is {len(plaintext)} bytes; the limit is {MAX_PLAINTEXT_BYTES}."
        )

    dek = secrets.token_bytes(KEY_BYTES)
    ciphertext_nonce = secrets.token_bytes(NONCE_BYTES)
    aad = _aad(tenant_id, secret_id, purpose)
    ciphertext = AESGCM(dek).encrypt(ciphertext_nonce, plaintext, aad)

    # The wrapped DEK is bound to the same context, so a DEK cannot be paired with a
    # different row's ciphertext.
    dek_nonce = secrets.token_bytes(NONCE_BYTES)
    wrapped_dek = AESGCM(keyring.get(keyring.active_id)).encrypt(dek_nonce, dek, aad)

    return SealedSecret(
        kek_id=keyring.active_id,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        ciphertext=ciphertext,
        ciphertext_nonce=ciphertext_nonce,
    )


def open_secret(
    keyring: KeyRing,
    sealed: SealedSecret,
    *,
    tenant_id: UUID | None,
    secret_id: UUID,
    purpose: str,
) -> bytes:
    """Decrypts a stored credential.

    Returns bytes, not str: the value is a credential to be handed to a client, not a
    string to be formatted into a message or a log line.
    """
    aad = _aad(tenant_id, secret_id, purpose)
    try:
        dek = AESGCM(keyring.get(sealed.kek_id)).decrypt(
            sealed.dek_nonce, sealed.wrapped_dek, aad
        )
    except InvalidTag as exc:
        raise EnvelopeError("Stored secret failed authentication (key or context).") from exc

    try:
        return AESGCM(dek).decrypt(sealed.ciphertext_nonce, sealed.ciphertext, aad)
    except InvalidTag as exc:
        raise EnvelopeError("Stored secret failed authentication (ciphertext).") from exc


def rewrap(
    keyring: KeyRing,
    sealed: SealedSecret,
    *,
    tenant_id: UUID | None,
    secret_id: UUID,
    purpose: str,
) -> SealedSecret:
    """Re-encrypts the DEK under the active KEK, leaving the ciphertext untouched.

    This is what makes key rotation cheap: the credential itself is never decrypted and
    re-encrypted, only the small key that protects it. Already-active rows are returned
    unchanged so a rotation pass is idempotent and can be re-run after an interruption.
    """
    if sealed.kek_id == keyring.active_id:
        return sealed

    dek = AESGCM(keyring.get(sealed.kek_id)).decrypt(
        sealed.dek_nonce, sealed.wrapped_dek, _aad(tenant_id, secret_id, purpose)
    )
    dek_nonce = secrets.token_bytes(NONCE_BYTES)
    wrapped = AESGCM(keyring.get(keyring.active_id)).encrypt(
        dek_nonce, dek, _aad(tenant_id, secret_id, purpose)
    )
    return SealedSecret(
        kek_id=keyring.active_id,
        wrapped_dek=wrapped,
        dek_nonce=dek_nonce,
        ciphertext=sealed.ciphertext,
        ciphertext_nonce=sealed.ciphertext_nonce,
    )
