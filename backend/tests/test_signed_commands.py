"""Signed device commands - real RS256 round trip, tamper detection, expiry, and
not-yet-valid, the same discipline test_tokens.py already applies to access tokens.
Needs no infra (Postgres/Redis) - `settings` (conftest.py) provides a real RSA keypair.
"""
from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest

from csense_shared.security.signed_commands import (
    COMMAND_AUDIENCE,
    SignedCommandError,
    sign_command,
    verify_signed_command,
)


def _sign(settings, **overrides):
    now = time.time()
    kwargs = {
        "settings": settings,
        "command_id": uuid.uuid4(),
        "edge_device_id": uuid.uuid4(),
        "command_type": "config.apply",
        "payload": {"pipeline_version_id": str(uuid.uuid4())},
        "idempotency_key": "key-1",
        "not_before": now,
        "expires_at": now + 3600,
    }
    kwargs.update(overrides)
    return kwargs, sign_command(**kwargs)


def test_a_valid_command_verifies_to_what_it_was_signed_for(settings):
    kwargs, token = _sign(settings)
    claims = verify_signed_command(token, settings=settings)

    assert claims.command_id == kwargs["command_id"]
    assert claims.edge_device_id == kwargs["edge_device_id"]
    assert claims.command_type == "config.apply"
    assert claims.payload == kwargs["payload"]
    assert claims.idempotency_key == "key-1"


def test_a_tampered_payload_fails_verification(settings):
    """The payload is inside the signed claims, not appended alongside them - changing
    even one byte after signing must invalidate the signature, not just the parsed
    value."""
    _, token = _sign(settings)
    header, payload_b64, signature = token.split(".")
    tampered_claims = pyjwt.utils.base64url_decode(payload_b64 + "==").replace(b"config.apply", b"config.evil")
    tampered_payload_b64 = pyjwt.utils.base64url_encode(tampered_claims).decode().rstrip("=")
    tampered_token = f"{header}.{tampered_payload_b64}.{signature}"

    with pytest.raises(SignedCommandError):
        verify_signed_command(tampered_token, settings=settings)


def test_an_expired_command_is_refused(settings):
    now = time.time()
    _, token = _sign(settings, not_before=now - 7200, expires_at=now - 3600)
    with pytest.raises(SignedCommandError):
        verify_signed_command(token, settings=settings)


def test_a_not_yet_valid_command_is_refused(settings):
    now = time.time()
    _, token = _sign(settings, not_before=now + 3600, expires_at=now + 7200)
    with pytest.raises(SignedCommandError):
        verify_signed_command(token, settings=settings)


def test_wrong_audience_is_refused(settings):
    """A command envelope must not be usable as anything else that happens to share the
    signing key - the same isolation test_tokens.py's own audience test proves for
    access tokens."""
    now = time.time()
    payload = {
        "iss": settings.jwt_issuer,
        "aud": "csense-customer",  # not COMMAND_AUDIENCE
        "command_id": str(uuid.uuid4()),
        "edge_device_id": str(uuid.uuid4()),
        "command_type": "config.apply",
        "payload": {},
        "idempotency_key": "key-1",
        "iat": int(now),
        "nbf": int(now),
        "exp": int(now + 3600),
    }
    token = pyjwt.encode(payload, settings.jwt_private_key, algorithm=settings.jwt_algorithm)
    with pytest.raises(SignedCommandError):
        verify_signed_command(token, settings=settings)


def test_signed_with_a_different_key_is_refused(settings):
    """A command signed by anything other than this deployment's own private key must
    never verify, however well-formed it otherwise looks."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    now = time.time()
    payload = {
        "iss": settings.jwt_issuer,
        "aud": COMMAND_AUDIENCE,
        "command_id": str(uuid.uuid4()),
        "edge_device_id": str(uuid.uuid4()),
        "command_type": "config.apply",
        "payload": {},
        "idempotency_key": "key-1",
        "iat": int(now),
        "nbf": int(now),
        "exp": int(now + 3600),
    }
    token = pyjwt.encode(payload, other_pem, algorithm=settings.jwt_algorithm)
    with pytest.raises(SignedCommandError):
        verify_signed_command(token, settings=settings)
