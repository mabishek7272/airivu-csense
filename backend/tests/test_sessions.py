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


async def test_a_token_more_than_the_generation_cap_stale_is_rejected_on_reuse(
    redis_client, test_settings, monkeypatch
):
    """The genuine-replay case that must stay strict even under the new multi-generation
    design: a token from BEFORE the bounded grace history's own cap is still rejected and
    still destroys the session.

    Prior to 2026-09-09's multi-generation fix, this test rotated exactly twice and then
    replayed the original (by-then two-generations-stale) token, asserting rejection - that
    assertion encoded the OLD bug, not a real security property: the old single-slot design
    could only ever remember one generation back, so *any* second rotation made an older
    straggler unrecoverable regardless of how little time had passed. That is exactly the
    gap the security review reproduced (a legitimate straggler of rotation N getting nuked
    by rotation N+1 landing first) and the multi-generation fix intentionally closes it - a
    token that is only 2 generations stale, still within its own grace TTL and within the
    generation cap, is now correctly *honoured*, not rejected (see
    `test_generation_0_stragglers_survive_a_second_legitimate_rotation` below).

    What must still genuinely reject is a token stale enough to have fallen out of the
    bounded history altogether: more rotations than `MAX_GRACE_GENERATIONS` have happened
    since it was current. Monkeypatch the cap down so this is deterministic and fast rather
    than needing five-plus real rotations.
    """
    import csense_shared.security.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "MAX_GRACE_GENERATIONS", 2)
    session_id, raw_token = await _new_session(redis_client, test_settings)

    current_token = raw_token
    for _ in range(4):  # 4 rotations > cap of 2: the original generation gets evicted
        record = await rotate_session(
            redis_client, test_settings, session_id=session_id, presented_token=current_token
        )
        assert record is not None
        current_token = record["new_refresh_token"]

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


# --- The security review's gap: a SECOND legitimate rotation must not clobber the FIRST's
# --- still-live grace generation ---------------------------------------------------------

async def test_generation_0_stragglers_survive_a_second_legitimate_rotation(redis_client, test_settings):
    """Deterministic (sequential, no gather) reproduction of the actual gap the review
    found, isolated from concurrency noise: rotation 0->1 completes, THEN rotation 1->2
    completes for real, and only AFTER THAT does a straggler of the FIRST rotation present
    its now-two-generations-stale token. Under the old single-overwritten-slot design this
    always failed (rotation 1->2 clobbered rotation 0->1's grace entry, so this presentation
    matched neither current nor grace and destroyed the session). Under the new bounded
    multi-generation design it must succeed, and must hand back the actual current
    (post-rotation-1->2) token, not the stale generation-1 token."""
    session_id, raw_token = await _new_session(redis_client, test_settings)

    first = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )
    assert first is not None
    second = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=first["new_refresh_token"]
    )
    assert second is not None

    straggler = await rotate_session(
        redis_client, test_settings, session_id=session_id, presented_token=raw_token
    )

    assert straggler is not None
    # Must converge on the CURRENT token established by the second rotation, not the
    # already-superseded generation-1 token.
    assert straggler["new_refresh_token"] == second["new_refresh_token"]
    assert await redis_client.exists(_session_key(test_settings, session_id)) == 1

    follow_up = await rotate_session(
        redis_client, test_settings, session_id=session_id,
        presented_token=straggler["new_refresh_token"],
    )
    assert follow_up is not None


async def test_generation_0_and_generation_2_stragglers_race_without_destroying_the_session(
    redis_client, test_settings
):
    """The review's own reproduction, run for real: this is the exact scenario that
    destroyed the session in 4 of 8 trials against the old single-slot design (real
    Redis, real `asyncio.gather`, no mocks) - a burst of generation-0 stragglers racing a
    burst of generation-2 (current) callers, AFTER two real sequential rotations have
    already produced those two generations. Every trial, without exception, must leave the
    session alive with every caller holding a token that remains usable afterward.

    Run many trials (not one) precisely because the bug this guards against is a race: a
    single passing run proves nothing about the fix actually being deterministic, the same
    way the original review needed ~8 trials before it observed a failure. TRIALS=25 with a
    20-way burst each (matching the review's 10-vs-10 shape) gives 500 concurrent
    rotate_session calls total across fresh sessions - if the multi-generation fix has any
    remaining race, this is enough attempts to surface it, not just get lucky once.
    """
    TRIALS = 25
    STRAGGLERS_PER_GENERATION = 10

    destroyed_trials = 0
    null_result_trials = 0

    for _ in range(TRIALS):
        session_id, raw_token = await _new_session(redis_client, test_settings)

        gen0_token = raw_token
        gen1 = await rotate_session(
            redis_client, test_settings, session_id=session_id, presented_token=gen0_token
        )
        assert gen1 is not None
        gen2 = await rotate_session(
            redis_client, test_settings, session_id=session_id, presented_token=gen1["new_refresh_token"]
        )
        assert gen2 is not None
        gen2_token = gen2["new_refresh_token"]

        # Now the real race: generation-0 stragglers (two rotations stale) vs. callers
        # presenting the genuinely-current generation-2 token, all in flight together.
        results = await asyncio.gather(
            *[
                rotate_session(redis_client, test_settings, session_id=session_id, presented_token=gen0_token)
                for _ in range(STRAGGLERS_PER_GENERATION)
            ],
            *[
                rotate_session(redis_client, test_settings, session_id=session_id, presented_token=gen2_token)
                for _ in range(STRAGGLERS_PER_GENERATION)
            ],
        )

        if any(r is None for r in results):
            null_result_trials += 1
        session_alive = await redis_client.exists(_session_key(test_settings, session_id)) == 1
        if not session_alive:
            destroyed_trials += 1
            continue

        # Every non-None result must be usable afterward - i.e. genuinely "currently
        # valid", not just non-None. Checking each unique token once is sufficient and
        # avoids the follow-up calls themselves interfering with each other's generations.
        for token in {r["new_refresh_token"] for r in results if r is not None}:
            follow_up = await rotate_session(
                redis_client, test_settings, session_id=session_id, presented_token=token
            )
            assert follow_up is not None, (
                f"a token returned by the generation-0/generation-2 race was not usable "
                f"afterward: {token!r}"
            )

    # The headline claim: this must hold EVERY trial, not "usually" - report the real
    # counts rather than silently averaging over a rare failure.
    assert destroyed_trials == 0, f"session destroyed in {destroyed_trials}/{TRIALS} trials"
    assert null_result_trials == 0, f"a caller got None in {null_result_trials}/{TRIALS} trials"


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
