"""Redis-backed fixed-window rate limiting + usage metering (csense_shared.security.
rate_limit). Needs a real Redis; skipped otherwise - same shape as test_ws_tickets.py.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.rate_limit import (
    RateLimitExceededError,
    check_and_increment,
    get_usage,
    record_usage,
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
        environment=f"rate-limit-test-{uuid.uuid4().hex[:8]}",  # its own namespace, so a
        # leftover key from a crashed run can never collide with a real one
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_calls_under_the_limit_all_succeed(redis_client, test_settings):
    client_id = uuid.uuid4()
    for _ in range(5):
        await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=5, at=1000.0)


async def test_the_call_that_crosses_the_limit_is_refused(redis_client, test_settings):
    client_id = uuid.uuid4()
    for _ in range(3):
        await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=3, at=1000.0)

    with pytest.raises(RateLimitExceededError) as exc_info:
        await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=3, at=1000.0)
    assert exc_info.value.limit == 3
    assert exc_info.value.retry_after_seconds > 0


async def test_a_refused_call_still_counts_against_the_window(redis_client, test_settings):
    """A client hammering the limit does not get a free extra request each time it is
    told no - see the module's own docstring for why."""
    client_id = uuid.uuid4()
    await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=1, at=1000.0)

    refusals = 0
    for _ in range(3):
        try:
            await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=1, at=1000.0)
        except RateLimitExceededError:
            refusals += 1
    assert refusals == 3


async def test_different_clients_have_independent_windows(redis_client, test_settings):
    a, b = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await check_and_increment(redis_client, test_settings, client_id=a, limit_per_minute=3, at=1000.0)

    # b's window is untouched by a's - this must not raise.
    await check_and_increment(redis_client, test_settings, client_id=b, limit_per_minute=3, at=1000.0)


async def test_a_new_minute_window_resets_the_count(redis_client, test_settings):
    client_id = uuid.uuid4()
    for _ in range(3):
        await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=3, at=1000.0)

    # 90 seconds later is a different 60-second window - must not raise.
    await check_and_increment(redis_client, test_settings, client_id=client_id, limit_per_minute=3, at=1090.0)


async def test_usage_is_recorded_and_read_back_for_the_right_date(redis_client, test_settings):
    """`get_usage` always reads the last N days from the real current time (a "my usage
    this past week" view has no reason to take an `at` override) - so this write has to
    land in *today's* bucket too, not a fixed historical instant, to be visible."""
    client_id = uuid.uuid4()

    await record_usage(redis_client, test_settings, client_id=client_id)
    await record_usage(redis_client, test_settings, client_id=client_id)

    usage = await get_usage(redis_client, test_settings, client_id=client_id, days=1)
    assert sum(usage.values()) == 2


async def test_usage_for_a_client_with_no_activity_is_all_zero(redis_client, test_settings):
    client_id = uuid.uuid4()
    usage = await get_usage(redis_client, test_settings, client_id=client_id, days=3)
    assert len(usage) == 3
    assert all(count == 0 for count in usage.values())
