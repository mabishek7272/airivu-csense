"""WebSocket auth tickets: single-use, short-lived, and never a stand-in for a real
access token check. Needs a real Redis; skipped otherwise.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.ws_tickets import consume_ws_ticket, create_ws_ticket

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
        environment=f"ws-ticket-test-{uuid.uuid4().hex[:8]}",  # its own namespace, so a
        # leftover key from a crashed run can never collide with a real one
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        token_audience="csense-customer", permissions=frozenset({"incident.read"}),
    )


async def test_a_valid_ticket_resolves_to_the_context_it_was_minted_for(redis_client, test_settings):
    context = _context()
    ticket = await create_ws_ticket(redis_client, test_settings, context=context)

    resolved = await consume_ws_ticket(redis_client, test_settings, ticket)

    assert resolved is not None
    assert resolved.tenant_id == context.tenant_id
    assert resolved.user_id == context.user_id
    assert resolved.membership_id == context.membership_id
    assert resolved.permissions == context.permissions


async def test_a_ticket_is_single_use(redis_client, test_settings):
    """The whole reason this exists rather than a plain query-param token: a ticket
    captured in a log or by a proxy in between must not be replayable for a second
    connection."""
    ticket = await create_ws_ticket(redis_client, test_settings, context=_context())

    first = await consume_ws_ticket(redis_client, test_settings, ticket)
    second = await consume_ws_ticket(redis_client, test_settings, ticket)

    assert first is not None
    assert second is None


async def test_an_unknown_ticket_resolves_to_nothing(redis_client, test_settings):
    resolved = await consume_ws_ticket(redis_client, test_settings, "not-a-real-ticket")
    assert resolved is None


async def test_a_ticket_expires_on_its_own(redis_client, test_settings, monkeypatch):
    """Not just single-use: a ticket that is minted and never used at all must not sit
    around indefinitely either - it disappears within seconds even if nobody ever tries
    it, the same way the real access token this stands in for cannot."""
    import csense_shared.security.ws_tickets as ws_tickets_module

    monkeypatch.setattr(ws_tickets_module, "TICKET_TTL_SECONDS", 1)
    ticket = await create_ws_ticket(redis_client, test_settings, context=_context())

    key = ws_tickets_module._ticket_key(test_settings, ticket)
    ttl = await redis_client.ttl(key)
    assert 0 < ttl <= 1


async def test_tickets_from_different_tenants_do_not_collide(redis_client, test_settings):
    a, b = _context(), _context()
    ticket_a = await create_ws_ticket(redis_client, test_settings, context=a)
    ticket_b = await create_ws_ticket(redis_client, test_settings, context=b)

    resolved_a = await consume_ws_ticket(redis_client, test_settings, ticket_a)
    resolved_b = await consume_ws_ticket(redis_client, test_settings, ticket_b)

    assert resolved_a.tenant_id == a.tenant_id
    assert resolved_b.tenant_id == b.tenant_id
