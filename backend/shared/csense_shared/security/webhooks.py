"""Webhook payload signing and verification - CHECKLIST's "Webhook signing,
verification, replay protection".

The scheme (timestamp + HMAC-SHA256 over `{timestamp}.{body}`, both carried in one
header) is the same shape Stripe/GitHub-style webhook signing already uses - not
reinvented, because a customer integrating against this deployment almost certainly
already has a verifier for exactly this shape from another vendor.

**Replay protection is the timestamp window, not a separate mechanism**: a signature is
only valid within `tolerance_seconds` of "now" (`verify_signature`'s own clock, i.e. the
receiver's clock) - a captured, replayed request becomes unverifiable the moment it ages
past the window, without needing a receiver-side seen-nonce store (this deployment is the
*sender*; a nonce store would have to live on the customer's own receiver, which is their
implementation to build against this same header shape, not ours).
"""
from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-CSense-Signature"
DEFAULT_TOLERANCE_SECONDS = 5 * 60


class WebhookSignatureError(Exception):
    """Raised by `verify_signature` for a missing, malformed, tampered, or expired
    signature. Every failure looks the same to a caller checking a boolean/exception,
    the same "don't let a caller treat one failure mode as safer than another" discipline
    `signed_commands.py` and `deps_agent.py` already both follow."""


def _digest(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()


def sign_payload(secret: str, body: bytes, *, at: float | None = None) -> str:
    """Returns the full header value: `t=<unix ts>,v1=<hex hmac>` - both the timestamp
    and the digest travel together, since verification needs the timestamp the digest
    was actually computed over, not the time the request happens to arrive."""
    timestamp = int(at if at is not None else time.time())
    digest = _digest(secret, timestamp, body)
    return f"t={timestamp},v1={digest}"


def verify_signature(
    secret: str, body: bytes, header_value: str, *, tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS, at: float | None = None
) -> None:
    """Raises `WebhookSignatureError` on any failure; returns normally (no return value)
    on success - the same "verify raises, doesn't return a bool a caller might forget to
    check" shape `signed_commands.py`'s own `verify_signed_command` uses."""
    parts: dict[str, str] = {}
    for chunk in header_value.split(","):
        key, _, value = chunk.partition("=")
        if key and value:
            parts[key] = value

    if "t" not in parts or "v1" not in parts:
        raise WebhookSignatureError("Malformed signature header - expected 't=...,v1=...'.")

    try:
        timestamp = int(parts["t"])
    except ValueError as exc:
        raise WebhookSignatureError("Malformed timestamp in signature header.") from exc

    now = at if at is not None else time.time()
    if abs(now - timestamp) > tolerance_seconds:
        raise WebhookSignatureError(
            f"Signature timestamp is outside the {tolerance_seconds}s tolerance window - "
            "either a replayed request or a badly drifted clock."
        )

    expected = _digest(secret, timestamp, body)
    if not hmac.compare_digest(expected, parts["v1"]):
        raise WebhookSignatureError("Signature does not match - wrong secret, or the body was tampered with.")
