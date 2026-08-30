"""Webhook signing/verification - pure functions, no infra needed. Same discipline
test_signed_commands.py already applies: a real round trip, tamper detection, and expiry,
not just "the happy path returns without raising".
"""
from __future__ import annotations

import pytest

from csense_shared.security.webhooks import WebhookSignatureError, sign_payload, verify_signature

SECRET = "whsec_test_secret_do_not_use_in_production"


def test_a_valid_signature_verifies():
    body = b'{"event":"incident.created","incident_id":"abc-123"}'
    header = sign_payload(SECRET, body, at=1_700_000_000)
    verify_signature(SECRET, body, header, at=1_700_000_000)  # does not raise


def test_a_tampered_body_fails_verification():
    body = b'{"event":"incident.created","incident_id":"abc-123"}'
    header = sign_payload(SECRET, body, at=1_700_000_000)
    tampered_body = b'{"event":"incident.created","incident_id":"different-999"}'
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, tampered_body, header, at=1_700_000_000)


def test_the_wrong_secret_fails_verification():
    body = b"payload"
    header = sign_payload(SECRET, body, at=1_700_000_000)
    with pytest.raises(WebhookSignatureError):
        verify_signature("a-completely-different-secret", body, header, at=1_700_000_000)


def test_an_expired_signature_is_refused():
    body = b"payload"
    header = sign_payload(SECRET, body, at=1_700_000_000)
    # 10 minutes later, outside the default 5-minute tolerance.
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, body, header, at=1_700_000_000 + 600)


def test_a_signature_from_the_future_is_also_refused():
    """Replay protection cuts both ways - a signature timestamped far in the future is
    just as suspicious as a stale one, not merely "not yet expired"."""
    body = b"payload"
    header = sign_payload(SECRET, body, at=1_700_000_600)
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, body, header, at=1_700_000_000)


def test_within_tolerance_still_verifies():
    body = b"payload"
    header = sign_payload(SECRET, body, at=1_700_000_000)
    verify_signature(SECRET, body, header, at=1_700_000_000 + 240)  # 4 minutes - does not raise


def test_malformed_header_is_rejected_not_crashed():
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, b"payload", "not-a-real-header")
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, b"payload", "")
    with pytest.raises(WebhookSignatureError):
        verify_signature(SECRET, b"payload", "t=not-a-number,v1=abc")


def test_signature_is_deterministic_for_the_same_inputs():
    """Not a security property by itself, but a correctness one: the same secret, body,
    and timestamp must always produce the same signature, or a receiver's own
    from-scratch reimplementation of this scheme could never match it."""
    body = b"payload"
    header1 = sign_payload(SECRET, body, at=1_700_000_000)
    header2 = sign_payload(SECRET, body, at=1_700_000_000)
    assert header1 == header2
