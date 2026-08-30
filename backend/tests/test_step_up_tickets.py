"""Step-up recency marker: checked-not-consumed, unlike invitation_tickets.py's single-use
GETDEL shape - see step_up_tickets.py's own docstring for why. Needs a real Redis; skipped
otherwise. Mirrors test_invitation_tickets.py's own shape.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.step_up_tickets import has_recent_step_up, mark_step_up_verified

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
        environment=f"step-up-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_no_verification_means_not_recently_verified(redis_client, test_settings):
    principal_id = uuid.uuid4()
    assert not await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)


async def test_a_marked_verification_is_recent(redis_client, test_settings):
    principal_id = uuid.uuid4()
    await mark_step_up_verified(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)
    assert await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)


async def test_checking_does_not_consume_it(redis_client, test_settings):
    """Unlike an invitation ticket, a step-up verification must cover more than one
    action within its window - checking it must not burn it."""
    principal_id = uuid.uuid4()
    await mark_step_up_verified(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)

    first = await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)
    second = await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)

    assert first
    assert second


async def test_different_principals_are_isolated(redis_client, test_settings):
    verified_principal, unverified_principal = uuid.uuid4(), uuid.uuid4()
    await mark_step_up_verified(redis_client, test_settings, scope="admin-high-risk", principal_id=verified_principal)

    assert await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=verified_principal)
    assert not await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=unverified_principal)


async def test_different_scopes_are_isolated(redis_client, test_settings):
    """A step-up proven for one scope must not silently cover an unrelated one, in case
    scopes are ever split further than the single 'admin-high-risk' this pass uses."""
    principal_id = uuid.uuid4()
    await mark_step_up_verified(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)

    assert await has_recent_step_up(redis_client, test_settings, scope="admin-high-risk", principal_id=principal_id)
    assert not await has_recent_step_up(redis_client, test_settings, scope="some-other-scope", principal_id=principal_id)
