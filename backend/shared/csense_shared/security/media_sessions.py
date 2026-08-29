"""Short-lived signed sessions that authorize one camera's live view through MediaMTX.

Deliberately **not** `ws_tickets.py`'s single-use GETDEL shape, even though the two look
alike at first glance (both are opaque Redis-backed tokens with a short TTL, minted by an
authenticated REST call). A WS ticket authorizes exactly one handshake and is consumed on
first use. A live-view session has to survive *repeated* authorization checks over a real
viewing duration instead - MediaMTX calls the HTTP auth webhook (TRD §14, verified against
the real contract) on every connection attempt, and a WHEP negotiation or an HLS player's
periodic segment/playlist fetches can mean more than one. Deleting the session on its first
check would end the stream a few seconds after it started. So this checks (`GET`, not
`GETDEL`) and relies on the TTL alone to expire it.

`protocol` is part of the stored record on purpose: an `hls` session must not authorize the
`webrtc` path for the same camera, or vice versa. They are not interchangeable - `webrtc`
also drives a real transcode process (see `media.py`), which costs CPU `hls` never does.
"""
from __future__ import annotations

import json
import secrets
import uuid

import redis.asyncio as redis

from csense_shared.config import Settings

MEDIA_SESSION_TTL_SECONDS = 600


class MediaSession:
    __slots__ = ("tenant_id", "camera_id", "path", "protocol")

    def __init__(self, *, tenant_id: uuid.UUID, camera_id: uuid.UUID, path: str, protocol: str) -> None:
        self.tenant_id = tenant_id
        self.camera_id = camera_id
        self.path = path
        self.protocol = protocol


def _session_key(settings: Settings, token: str) -> str:
    return f"cs:{settings.environment}:media-session:{token}"


async def create_media_session(
    redis_client: redis.Redis,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    path: str,
    protocol: str,
) -> str:
    token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "camera_id": str(camera_id),
            "path": path,
            "protocol": protocol,
        }
    )
    await redis_client.set(_session_key(settings, token), payload, ex=MEDIA_SESSION_TTL_SECONDS)
    return token


async def check_media_session(
    redis_client: redis.Redis, settings: Settings, token: str
) -> MediaSession | None:
    """Reads (never deletes) the session record - see the module docstring for why this
    differs from `ws_tickets.consume_ws_ticket`. Returns None for a missing, expired, or
    malformed token, indistinguishably - either way, no session was verified."""
    raw = await redis_client.get(_session_key(settings, token))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return MediaSession(
            tenant_id=uuid.UUID(data["tenant_id"]),
            camera_id=uuid.UUID(data["camera_id"]),
            path=data["path"],
            protocol=data["protocol"],
        )
    except (KeyError, ValueError, TypeError):
        return None
