"""RFC 6238 TOTP - pure functions, no infra needed. Verified against a code generated at
a fixed instant rather than "generate then immediately verify" alone, so a bug that
happened to cancel out between generation and verification (e.g. both using the wrong
step size) wouldn't hide behind a passing test.
"""
from __future__ import annotations

from csense_shared.security.totp import (
    STEP_SECONDS,
    generate_totp_secret,
    provisioning_uri,
    totp_now,
    verify_totp,
)

# RFC 6238 Appendix B's own SHA-1 test vector: secret "12345678901234567890" (ASCII,
# base32-encoded below), time 59s -> 8-digit code "94287082". This module always
# generates 6 digits (Google Authenticator/Authy's near-universal default), so the
# comparison is against the RFC vector's own last 6 digits, not the full 8 - the HOTP
# truncation in RFC 4226 Sec 5.3 takes the low-order decimal digits, so a correct 6-digit
# implementation necessarily agrees with a correct 8-digit one on those digits. Using the
# RFC's own vector, not a self-generated one, is what proves the HMAC/counter/truncation
# steps are actually RFC-conformant rather than merely self-consistent.
_RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_RFC_8_DIGIT_CODE_AT_59S = "94287082"


def test_rfc_6238_test_vector():
    assert totp_now(_RFC_SECRET_B32, at=59) == _RFC_8_DIGIT_CODE_AT_59S[-6:]


def test_generated_secret_is_usable():
    secret = generate_totp_secret()
    code = totp_now(secret, at=1_700_000_000)
    assert verify_totp(secret, code, at=1_700_000_000)


def test_wrong_code_is_rejected():
    secret = generate_totp_secret()
    real_code = totp_now(secret, at=1_700_000_000)
    wrong_code = f"{(int(real_code) + 1) % 1_000_000:06d}"
    assert not verify_totp(secret, wrong_code, at=1_700_000_000)


def test_window_tolerates_clock_drift_within_bounds():
    secret = generate_totp_secret()
    now = 1_700_000_000
    code = totp_now(secret, at=now)
    # A code generated `now` still verifies when checked one step (30s) later or earlier.
    assert verify_totp(secret, code, at=now + STEP_SECONDS, window=1)
    assert verify_totp(secret, code, at=now - STEP_SECONDS, window=1)


def test_window_rejects_drift_beyond_bounds():
    secret = generate_totp_secret()
    now = 1_700_000_000
    code = totp_now(secret, at=now)
    assert not verify_totp(secret, code, at=now + 5 * STEP_SECONDS, window=1)


def test_malformed_code_is_rejected_not_crashed():
    secret = generate_totp_secret()
    assert not verify_totp(secret, "not-a-code")
    assert not verify_totp(secret, "12345")  # too short
    assert not verify_totp(secret, "")


def test_provisioning_uri_carries_the_real_secret():
    secret = generate_totp_secret()
    uri = provisioning_uri(secret, account_name="owner@example.com", issuer="AIRIVU CSense")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=AIRIVU" in uri
