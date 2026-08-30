"""Signed device commands - CHECKLIST's "Signed commands with expiry/idempotency
(desired-state push to the device)".

Reuses the exact same asymmetric keypair `tokens.py` already signs access tokens with
(`settings.jwt_private_key`/`jwt_public_key`, RS256) - a command is the same kind of
assertion an access token is ("the real control plane says so"), just addressed to a
device instead of a person, so it gets the same signing mechanism rather than a second,
parallel one. `exp`/`nbf` are enforced by the JWT library itself on verify, the same way
`decode_access_token` already relies on it - an edge device checking a command's signature
gets expiry and not-yet-valid handling for free, not as something it has to implement
itself.

**What this deliberately does not include**: an edge agent to consume it.
`edge/agent/` is still an empty directory - this is the server-side half of the feature,
built and proven correct (a real RS256 round trip, tamper detection, expiry) against
nothing but itself, the same honest-partial shape `csense_shared/onvif/discovery.py` is
(real, unit-tested, never wired to a live consumer because the consumer doesn't exist
yet). `backend/tenant_api/app/api/edge.py`'s own issue/poll/ack endpoints are real and
usable the moment a real agent exists to call them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt

from csense_shared.config import Settings

COMMAND_AUDIENCE = "csense-edge-command"


class SignedCommandError(Exception):
    """Raised for any invalid/expired/not-yet-valid/tampered command envelope. A device
    (or, in this session's absence of one, anything acting on its behalf) must treat this
    as a refusal, never fall back to executing the command anyway."""


@dataclass(frozen=True)
class SignedCommandClaims:
    command_id: UUID
    edge_device_id: UUID
    command_type: str
    payload: dict[str, Any]
    idempotency_key: str


def sign_command(
    *,
    settings: Settings,
    command_id: UUID,
    edge_device_id: UUID,
    command_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    not_before: float,
    expires_at: float,
    key_id: str = "local-dev-1",
) -> str:
    claims: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": COMMAND_AUDIENCE,
        "command_id": str(command_id),
        "edge_device_id": str(edge_device_id),
        "command_type": command_type,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "iat": int(time.time()),
        "nbf": int(not_before),
        "exp": int(expires_at),
    }
    return jwt.encode(
        claims, settings.jwt_private_key, algorithm=settings.jwt_algorithm, headers={"kid": key_id},
    )


def verify_signed_command(token: str, *, settings: Settings) -> SignedCommandClaims:
    """Verifies signature, issuer, audience, and the `nbf`/`exp` window - all in one
    `jwt.decode` call, the same as `decode_access_token` already does for a person's
    token. Any failure (bad signature, expired, not yet valid, wrong audience, tampered
    payload - changing even one byte of `payload` after signing invalidates the
    signature, since it's inside the signed claims, not appended alongside them) raises
    `SignedCommandError` uniformly; a caller does not get to distinguish "expired" from
    "forged" and treat one as safer than the other.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
            audience=COMMAND_AUDIENCE,
            issuer=settings.jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise SignedCommandError(str(exc)) from exc

    return SignedCommandClaims(
        command_id=UUID(payload["command_id"]),
        edge_device_id=UUID(payload["edge_device_id"]),
        command_type=payload["command_type"],
        payload=payload["payload"],
        idempotency_key=payload["idempotency_key"],
    )
