"""Media-view sessions: short-lived, checked-not-consumed, protocol- and path-scoped.

Needs a real Redis; skipped otherwise. See `csense_shared.security.media_sessions`'s own
docstring for why this is *not* `ws_tickets.py`'s single-use shape - the risk this file
pins is different: a session must survive repeated checks over a real viewing duration
(unlike a ticket), but must never authorize a different camera's path or the wrong
protocol just because a caller supplies a superficially-plausible request.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.media_sessions import check_media_session, create_media_session

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
        environment=f"media-session-test-{uuid.uuid4().hex[:8]}",
        postgres_password="test", redis_password="test",
        minio_root_user="test", minio_root_password="test",
        jwt_private_key_path="/dev/null", jwt_public_key_path="/dev/null",
    )


async def test_a_valid_session_resolves_to_what_it_was_minted_for(redis_client, test_settings):
    tenant_id, camera_id = uuid.uuid4(), uuid.uuid4()
    token = await create_media_session(
        redis_client, test_settings,
        tenant_id=tenant_id, camera_id=camera_id, path=f"{camera_id}-hls", protocol="hls",
    )

    session = await check_media_session(redis_client, test_settings, token)

    assert session is not None
    assert session.tenant_id == tenant_id
    assert session.camera_id == camera_id
    assert session.path == f"{camera_id}-hls"
    assert session.protocol == "hls"


async def test_a_session_survives_repeated_checks(redis_client, test_settings):
    """The whole reason this differs from ws_tickets.py's GETDEL: a live-view session has
    to authorize more than one connection attempt (MediaMTX's auth webhook fires per
    attempt, and HLS/WHEP can mean more than one) - unlike a WS ticket, checking it must
    not consume it."""
    camera_id = uuid.uuid4()
    token = await create_media_session(
        redis_client, test_settings,
        tenant_id=uuid.uuid4(), camera_id=camera_id, path=f"{camera_id}-hls", protocol="hls",
    )

    first = await check_media_session(redis_client, test_settings, token)
    second = await check_media_session(redis_client, test_settings, token)

    assert first is not None
    assert second is not None


async def test_an_unknown_token_resolves_to_nothing(redis_client, test_settings):
    session = await check_media_session(redis_client, test_settings, "not-a-real-token")
    assert session is None


async def test_a_session_expires_on_its_own(redis_client, test_settings, monkeypatch):
    import csense_shared.security.media_sessions as media_sessions_module

    monkeypatch.setattr(media_sessions_module, "MEDIA_SESSION_TTL_SECONDS", 1)
    camera_id = uuid.uuid4()
    token = await create_media_session(
        redis_client, test_settings,
        tenant_id=uuid.uuid4(), camera_id=camera_id, path=f"{camera_id}-hls", protocol="hls",
    )

    key = media_sessions_module._session_key(test_settings, token)
    ttl = await redis_client.ttl(key)
    assert 0 < ttl <= 1


async def test_sessions_for_different_cameras_do_not_collide(redis_client, test_settings):
    tenant_id = uuid.uuid4()
    camera_a, camera_b = uuid.uuid4(), uuid.uuid4()
    token_a = await create_media_session(
        redis_client, test_settings,
        tenant_id=tenant_id, camera_id=camera_a, path=f"{camera_a}-hls", protocol="hls",
    )
    token_b = await create_media_session(
        redis_client, test_settings,
        tenant_id=tenant_id, camera_id=camera_b, path=f"{camera_b}-hls", protocol="hls",
    )

    session_a = await check_media_session(redis_client, test_settings, token_a)
    session_b = await check_media_session(redis_client, test_settings, token_b)

    assert session_a.camera_id == camera_a
    assert session_a.path == f"{camera_a}-hls"
    assert session_b.camera_id == camera_b
    assert session_b.path == f"{camera_b}-hls"
    # The property that actually matters: camera A's session must not authorize camera B's
    # MediaMTX path, which is what the auth webhook itself checks (media.py).
    assert session_a.path != session_b.path


async def test_protocol_is_carried_and_distinguishable(redis_client, test_settings):
    """An hls session must not be usable to justify a webrtc path or vice versa - they
    cost genuinely different amounts (webrtc drives a real transcode process)."""
    camera_id = uuid.uuid4()
    hls_token = await create_media_session(
        redis_client, test_settings,
        tenant_id=uuid.uuid4(), camera_id=camera_id, path=f"{camera_id}-hls", protocol="hls",
    )
    webrtc_token = await create_media_session(
        redis_client, test_settings,
        tenant_id=uuid.uuid4(), camera_id=camera_id, path=f"{camera_id}-webrtc", protocol="webrtc",
    )

    hls_session = await check_media_session(redis_client, test_settings, hls_token)
    webrtc_session = await check_media_session(redis_client, test_settings, webrtc_token)

    assert hls_session.protocol == "hls"
    assert hls_session.path == f"{camera_id}-hls"
    assert webrtc_session.protocol == "webrtc"
    assert webrtc_session.path == f"{camera_id}-webrtc"
