"""RFC 6238 TOTP - implemented directly against the stdlib rather than pulling in a
dependency (`hmac`/`hashlib`/`base64`/`struct` are the entire algorithm; the codebase
already prefers a small, auditable implementation over a library for things this size -
`csense_shared/onvif/discovery.py` made the identical call for WS-Discovery).

30-second step, 6 digits, SHA-1 (the near-universal defaults every authenticator app -
Google Authenticator, Authy, 1Password - assumes unless told otherwise via the
provisioning URI's own query parameters, which this always sets explicitly rather than
relying on an app's default).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP_SECONDS = 30
DIGITS = 6


def generate_totp_secret() -> str:
    """A 160-bit secret, base32-encoded (no padding) - RFC 4226 §4's recommended length."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    # Base32 requires padding to a multiple of 8 chars to decode; the stored secret never
    # carries it (generate_totp_secret strips it), so it's re-added here rather than
    # forcing every caller to remember to.
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def totp_now(secret_b32: str, *, at: float | None = None) -> str:
    counter = int((at if at is not None else time.time()) // STEP_SECONDS)
    return _hotp(secret_b32, counter)


def verify_totp(secret_b32: str, code: str, *, at: float | None = None, window: int = 1) -> bool:
    """Accepts a code from the current step or `window` steps on either side, so a
    slightly-drifted clock (the realistic failure mode, not an attack) doesn't lock
    someone out. `window=1` at a 30s step means ±30s - not wide enough to matter for
    brute force (6 digits still means 1-in-a-million per guess, tried at most 3 times
    per real code)."""
    if not code.isdigit() or len(code) != DIGITS:
        return False
    now = at if at is not None else time.time()
    counter = int(now // STEP_SECONDS)
    return any(
        hmac.compare_digest(_hotp(secret_b32, counter + offset), code)
        for offset in range(-window, window + 1)
    )


def provisioning_uri(secret_b32: str, *, account_name: str, issuer: str) -> str:
    """`otpauth://` URI an authenticator app scans as a QR code or accepts as manual
    entry text - RFC unofficial but universally implemented format."""
    label = urllib.parse.quote(f"{issuer}:{account_name}")
    params = urllib.parse.urlencode(
        {"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{params}"
