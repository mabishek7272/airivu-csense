"""Team invitation tokens: single-use, long-lived, Redis-backed. Needs a real Redis;
skipped otherwise. Mirrors test_ws_tickets.py's own shape - see invitation_tickets.py's
docstring for why this one's lifetime and Redis-key format are deliberately different.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.invitation_tickets import (
    consume_invitation_ticket,
    create_invitation_ticket,
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
        host="localhost", port=6379, password=os.environ["TEST_REDIS_PASSWORD"], decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        environment=f"invitation-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_a_valid_ticket_resolves_to_what_it_was_minted_for(redis_client, test_settings):
    membership_id, tenant_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = await create_invitation_ticket(
        redis_client, test_settings,
        membership_id=membership_id, tenant_id=tenant_id, user_id=user_id, email="new@example.com",
    )

    ticket = await consume_invitation_ticket(redis_client, test_settings, token)

    assert ticket is not None
    assert ticket.membership_id == membership_id
    assert ticket.tenant_id == tenant_id
    assert ticket.user_id == user_id
    assert ticket.email == "new@example.com"


async def test_a_ticket_is_single_use(redis_client, test_settings):
    """The whole reason accepting an invitation twice must not work: a link forwarded or
    left in a shared inbox must not let a second person claim the same membership."""
    token = await create_invitation_ticket(
        redis_client, test_settings,
        membership_id=uuid.uuid4(), tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), email="a@example.com",
    )

    first = await consume_invitation_ticket(redis_client, test_settings, token)
    second = await consume_invitation_ticket(redis_client, test_settings, token)

    assert first is not None
    assert second is None


async def test_an_unknown_token_resolves_to_nothing(redis_client, test_settings):
    ticket = await consume_invitation_ticket(redis_client, test_settings, "not-a-real-token")
    assert ticket is None


async def test_a_ticket_expires_on_its_own(redis_client, test_settings, monkeypatch):
    import csense_shared.security.invitation_tickets as invitation_tickets_module

    monkeypatch.setattr(invitation_tickets_module, "INVITATION_TTL_SECONDS", 1)
    token = await create_invitation_ticket(
        redis_client, test_settings,
        membership_id=uuid.uuid4(), tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), email="a@example.com",
    )

    key = invitation_tickets_module._ticket_key(test_settings, token)
    ttl = await redis_client.ttl(key)
    assert 0 < ttl <= 1


async def test_the_redis_key_embeds_the_raw_token_as_its_suffix(redis_client, test_settings):
    """Confirmed, not just asserted in a comment - this is the intended way to inspect a
    real, uncommitted invitation during development/e2e verification (`redis-cli KEYS
    "cs:{env}:invitation:*"`) without the API ever returning the token once an email
    actually sent for it."""
    import csense_shared.security.invitation_tickets as invitation_tickets_module

    token = await create_invitation_ticket(
        redis_client, test_settings,
        membership_id=uuid.uuid4(), tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), email="a@example.com",
    )
    expected_key = f"cs:{test_settings.environment}:invitation:{token}"
    assert invitation_tickets_module._ticket_key(test_settings, token) == expected_key
    assert await redis_client.exists(expected_key) == 1
