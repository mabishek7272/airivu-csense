import uuid

import pytest

from csense_shared.security.tokens import (
    AUDIENCE_CUSTOMER,
    AUDIENCE_PLATFORM,
    TokenError,
    decode_access_token,
    issue_access_token,
)


def test_access_token_roundtrip(settings):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    membership_id = uuid.uuid4()

    token = issue_access_token(
        settings=settings,
        user_id=user_id,
        audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id,
        membership_id=membership_id,
        permissions=frozenset({"tenant.user.manage"}),
        session_id="session-1",
    )
    claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_CUSTOMER)

    assert claims.subject_user_id == user_id
    assert claims.tenant_id == tenant_id
    assert claims.membership_id == membership_id
    assert "tenant.user.manage" in claims.permissions


def test_customer_token_rejected_by_platform_audience(settings):
    """This is the exact property Phase 1's exit gate (IMP-G1) requires: a
    customer-audience token must not be usable against the platform API."""
    token = issue_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        permissions=frozenset(),
        session_id="session-2",
    )
    with pytest.raises(TokenError):
        decode_access_token(token, settings=settings, expected_audience=AUDIENCE_PLATFORM)


def test_platform_token_has_no_tenant_claim(settings):
    """A platform token is minted without tenant_id/membership_id — there is no implicit
    tenant access to strip or forget to check."""
    token = issue_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        audience=AUDIENCE_PLATFORM,
        tenant_id=None,
        membership_id=None,
        permissions=frozenset({"organization.manage"}),
        session_id="session-3",
    )
    claims = decode_access_token(token, settings=settings, expected_audience=AUDIENCE_PLATFORM)
    assert claims.tenant_id is None
    assert claims.membership_id is None


def test_expired_token_rejected(settings):
    import time

    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "iss": settings.jwt_issuer,
        "aud": AUDIENCE_CUSTOMER,
        "sub": str(uuid.uuid4()),
        "iat": now - 700,
        "nbf": now - 700,
        "exp": now - 600,  # already expired
        "jti": str(uuid.uuid4()),
        "sid": "session-4",
        "perm": [],
    }
    expired_token = pyjwt.encode(payload, settings.jwt_private_key, algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError):
        decode_access_token(expired_token, settings=settings, expected_audience=AUDIENCE_CUSTOMER)
