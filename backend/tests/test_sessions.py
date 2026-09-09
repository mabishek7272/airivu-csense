"""Refresh-session rotation: the property that matters is that a genuine replay still
kills the session, while two legitimate concurrent callers presenting the same current
token (two browser tabs racing a silent refresh, a slow-network retry) do not - see
`csense_shared.security.sessions`'s own docstring for the grace-window design this pins.

Needs a real Redis; skipped otherwise, same convention as `test_media_session.py`. The
concurrency test uses real `asyncio.gather`, not sequential calls that happen to look
concurrent - a naive read-then-write implementation can pass a sequential version of this
test and still be racy under true concurrency, which is exactly the bug this module fixes.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.sessions import (
    _grace_key,
    _session_key,
    create_session,
    revoke_session,
    rotate_session,
)
from csense_shared.security.tokens import AUDIENCE_CUSTOMER

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
        environment=f"sessions-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def _new_session(redis_client, test_settings):
    session_id, raw_token = await create_session(
        redis_client, test_settings,
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        audience=AUDIENCE_CUSTOMER,
    )
    return session_id, raw_token


# --- Basics -----------------------------------------------------------------------------

async def test_a_freshly_created_session_rotates_on_first_use(redis_client, test_settings):
    session_id, raw_token = await _new_session(redis_client, test_settings)

    record = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )

    assert record is not None
    assert record["new_refresh_token"] != raw_token


async def test_an_unknown_session_id_resolves_to_nothing(redis_client, test_settings):
    record = await rotate_session(
        redis_client, test_settings, session_id=str(uuid.uuid4()), presented_token="whatever"
    )
    assert record is None


# --- Real replay must still be rejected, exactly as before ------------------------------

async def test_a_forged_token_is_rejected_and_destroys_the_session(redis_client, test_settings):
    session_id, raw_token = await _new_session(redis_client, test_settings)

    record = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token="not-the-real-token"
    )

    assert record is None
    # The whole point of strict rotation: a bad presentation kills the session family, not
    # just this one call.
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 0


async def test_a_token_already_rotated_away_is_rejected_on_reuse(redis_client, test_settings):
    """The exact scenario strict rotation exists to catch: presenting a token again after
    it has already been legitimately rotated once (a real replay, or a stale value from a
    tab that never got a chance to update its cookie)."""
    session_id, raw_token = await _new_session(redis_client, test_settings)
    first = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )
    assert first is not None

    # Rotate again, moving the "current" token forward - raw_token is now two rotations
    # stale, so it must not be honoured even by the grace window (which only covers the
    # *immediately* preceding token).
    second = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=first["new_refresh_token"]
    )
    assert second is not None

    replay = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )

    assert replay is None
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 0


async def test_a_stale_previous_token_is_rejected_once_the_grace_window_has_elapsed(
    redis_client, test_settings, monkeypatch
):
    """A real replay of the *immediately previous* token, arriving after the grace window
    has genuinely elapsed (waited out for real, not a mocked clock), must still be
    rejected and still revoke the session - the grace window is a few seconds, not
    unlimited tolerance."""
    import csense_shared.security.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "GRACE_WINDOW_SECONDS", 1)
    session_id, raw_token = await _new_session(redis_client, test_settings)

    rotated = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )
    assert rotated is not None

    await asyncio.sleep(1.5)  # let the real grace key expire in real Redis

    replay = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )

    assert replay is None
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 0


# --- The actual fix: concurrent legitimate use must not destroy the session -------------

async def test_the_immediately_previous_token_is_honoured_within_the_grace_window(
    redis_client, test_settings
):
    """Sequential version of the race: a straggler presents the just-superseded token a
    moment after a legitimate rotation. Must get back a *currently valid* token, and the
    session must survive."""
    session_id, raw_token = await _new_session(redis_client, test_settings)

    winner = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )
    assert winner is not None

    straggler = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )

    assert straggler is not None
    assert straggler["new_refresh_token"] == winner["new_refresh_token"]
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 1

    # And that shared token really is honoured by a subsequent real call.
    follow_up = await rotate_session(
        redis_client, test_settings, session_id=session_id,
        presented_token=straggler["new_refresh_token"],
    )
    assert follow_up is not None


async def test_two_real_concurrent_callers_presenting_the_same_current_token_both_succeed(
    redis_client, test_settings
):
    """The actual bug: two browser tabs (or a slow-network retry) racing
    `POST /auth/refresh` with the same current token at genuinely the same time - real
    `asyncio.gather`, so both `rotate_session` calls are in flight together, not run one
    after the other. Exactly one performs the real rotation; the other must land in the
    grace path rather than being treated as a replay. Neither call may destroy the
    session, and both returned tokens must remain valid afterwards."""
    session_id, raw_token = await _new_session(redis_client, test_settings)

    results = await asyncio.gather(
        rotate_session(redis_client, test_settings, session_id=session_id, presented_token=raw_token),
        rotate_session(redis_client, test_settings, session_id=session_id, presented_token=raw_token),
    )

    assert results[0] is not None
    assert results[1] is not None
    # The session must not have been destroyed by this interaction.
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 1

    # Both calls must have been handed back a token that is CURRENTLY valid - i.e. both
    # converge on the same current token rather than one caller being left holding a
    # token nothing will ever accept again.
    assert results[0]["new_refresh_token"] == results[1]["new_refresh_token"]

    follow_up = await rotate_session(
        redis_client, test_settings, session_id=session_id,
        presented_token=results[0]["new_refresh_token"],
    )
    assert follow_up is not None


async def test_many_concurrent_stragglers_all_succeed_without_destroying_the_session(
    redis_client, test_settings
):
    """More than two concurrent callers (e.g. several tabs) must not fare any worse than
    two - the grace key is checked, not consumed, so any number of stragglers within the
    window converge on the same current token."""
    session_id, raw_token = await _new_session(redis_client, test_settings)

    results = await asyncio.gather(
        *[
            rotate_session(redis_client, test_settings, session_id=session_id, presented_token=raw_token)
            for _ in range(5)
        ]
    )

    assert all(r is not None for r in results)
    tokens = {r["new_refresh_token"] for r in results}
    assert len(tokens) == 1  # every caller converged on the same currently-valid token
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 1


# --- Grace key hygiene --------------------------------------------------------------------

async def test_the_grace_key_carries_a_short_ttl_not_the_full_session_ttl(redis_client, test_settings):
    """A grace window is a small window, not 'the previous token stays valid forever' -
    the grace key must expire on its own short schedule, independent of the session's
    14-day refresh TTL."""
    session_id, raw_token = await _new_session(redis_client, test_settings)

    await rotate_session(redis_client, test_settings, session_id=session_id, presented_token=raw_token)

    ttl = await redis_client.ttl(_grace_key(test_settings, session_id))
    assert 0 < ttl <= 10


async def test_revoke_session_also_clears_the_grace_key(redis_client, test_settings):
    """Logout must not leave a grace key around that could later be used to resurrect
    session-shaped confusion, even though by itself it can't outlive the session's own
    revocation - defensive cleanup, matching the symmetry of create/rotate both touching
    both keys."""
    session_id, raw_token = await _new_session(redis_client, test_settings)
    await rotate_session(redis_client, test_settings, session_id=session_id, presented_token=raw_token)
    assert await redis_client.exists(_grace_key(test_settings, session_id)) == 1

    await revoke_session(redis_client, test_settings, session_id=session_id)

    assert await redis_client.exists(_session_key(test_settings, session_id)) == 0
    assert await redis_client.exists(_grace_key(test_settings, session_id)) == 0
