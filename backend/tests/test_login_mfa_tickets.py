"""Login-time MFA challenge tickets: single-use only on SUCCESS (unlike
invitation_tickets.py's GETDEL-on-first-read), with a bounded retry counter. Needs a real
Redis; skipped otherwise. Mirrors test_invitation_tickets.py's own shape.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.login_mfa_tickets import (
    MAX_VERIFY_ATTEMPTS,
    consume_login_mfa_ticket,
    create_login_mfa_ticket,
    peek_login_mfa_ticket,
    register_failed_attempt,
)

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TEST_REDIS_PASSWORD"), reason="TEST_REDIS_PASSWORD not set - skipping"
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture()
async def redis_client():
    client = redis.Redis(
        host="localhost", port=int(os.environ.get("TEST_REDIS_PORT", "6379")),
        password=os.environ["TEST_REDIS_PASSWORD"], decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        environment=f"login-mfa-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_a_valid_ticket_resolves_to_what_it_was_minted_for(redis_client, test_settings):
    user_id, tenant_id, membership_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, factor="totp",
        device_id="device-1", device_name="Pixel", platform="android", otp_hash=None,
    )

    ticket = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert ticket is not None
    assert ticket.user_id == user_id
    assert ticket.tenant_id == tenant_id
    assert ticket.membership_id == membership_id
    assert ticket.factor == "totp"
    assert ticket.device_id == "device-1"
    assert ticket.otp_hash is None


async def test_peeking_does_not_consume_the_ticket(redis_client, test_settings):
    """The whole reason this module exists instead of reusing invitation_tickets.py's
    GETDEL shape: a user must be able to retype a wrong code without losing the ticket."""
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="email_otp", device_id=None, device_name=None, platform=None, otp_hash="abc",
    )

    first = await peek_login_mfa_ticket(redis_client, test_settings, token)
    second = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert first is not None
    assert second is not None
    assert first.user_id == second.user_id


async def test_consume_deletes_the_ticket(redis_client, test_settings):
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    await consume_login_mfa_ticket(redis_client, test_settings, token)
    after = await peek_login_mfa_ticket(redis_client, test_settings, token)

    assert after is None


async def test_an_unknown_token_resolves_to_nothing(redis_client, test_settings):
    ticket = await peek_login_mfa_ticket(redis_client, test_settings, "not-a-real-token")
    assert ticket is None


async def test_failed_attempts_are_capped(redis_client, test_settings):
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    for _ in range(MAX_VERIFY_ATTEMPTS - 1):
        count = await register_failed_attempt(redis_client, test_settings, token)
        assert count < MAX_VERIFY_ATTEMPTS
        assert await peek_login_mfa_ticket(redis_client, test_settings, token) is not None

    # The attempt that reaches the cap deletes the ticket outright.
    final_count = await register_failed_attempt(redis_client, test_settings, token)
    assert final_count >= MAX_VERIFY_ATTEMPTS
    assert await peek_login_mfa_ticket(redis_client, test_settings, token) is None


async def test_a_ticket_expires_on_its_own(redis_client, test_settings, monkeypatch):
    import csense_shared.security.login_mfa_tickets as login_mfa_tickets_module

    monkeypatch.setattr(login_mfa_tickets_module, "LOGIN_MFA_TTL_SECONDS", 1)
    token = await create_login_mfa_ticket(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        factor="totp", device_id=None, device_name=None, platform=None, otp_hash=None,
    )

    ttl = await redis_client.ttl(login_mfa_tickets_module._ticket_key(test_settings, token))
    assert 0 < ttl <= 1
