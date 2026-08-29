"""Media session service: brokers short-lived, signed live-view sessions through MediaMTX.

TRD §14/§6.3: the Media Session Service issues a short-lived token after checking
membership, scope, camera status, privacy policy and licence; live media itself is never
routed through Tenant API processes - only this session-issuing call and the auth webhook
below are. Once authorized, the browser talks to MediaMTX directly (HLS segment fetches,
WebRTC WHEP negotiation + ICE/RTP over UDP) - never through this service.

Two protocols, genuinely different costs - see CHECKLIST.md and [[nvr-h265-constraint]] for
the real measurement behind this: the deployment's actual NVR streams H.265 only, which no
mainstream browser's WebRTC stack can decode, but which HLS carries natively.
- `hls`: relays the camera's real stream, no transcoding, defaults to the **mainstream**
  for quality (no cost difference between main/substream here - MediaMTX only relays).
- `webrtc`: needs a real transcode to H.264. Always transcodes the **substream**, never the
  mainstream (measured ~0.23 CPU cores/camera vs ~1.7) via a MediaMTX `runOnDemand` ffmpeg
  process that does not start until a WebRTC viewer actually connects.
"""
from __future__ import annotations

import logging
import shlex
import uuid
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cameras import load_camera
from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from app.services.camera_stream import CredentialUnreadableError, resolve_camera_rtsp_url
from csense_shared.config import Settings
from csense_shared.errors import ApiError
from csense_shared.security.media_sessions import (
    MEDIA_SESSION_TTL_SECONDS,
    check_media_session,
    create_media_session,
)
from csense_shared.security.outbound import BlockedAddressError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

CREDENTIAL_PURPOSE = "camera.rtsp"  # matches cameras.py's own constant for the same secret
MEDIAMTX_TIMEOUT = 5.0


class LiveSessionIn(BaseModel):
    protocol: str = Field(pattern="^(hls|webrtc)$")


class LiveSessionOut(BaseModel):
    path: str
    protocol: str
    token: str
    play_url: str
    expires_in: int


def _mediamtx_path_name(camera_id: uuid.UUID, protocol: str) -> str:
    return f"{camera_id}-{protocol}"


async def _put_mediamtx_path(settings: Settings, *, path_name: str, config: dict) -> None:
    """Always **replaces** the path config, never merely creates-if-missing.

    A camera's credentials or resolved address can change after the first session ever
    created this path (`PUT /cameras/{id}/credentials` rotates the secret independently) -
    a config baked in once and never refreshed would keep a live-view path pointed at a
    stale password indefinitely. Replacing on every session request costs one small local
    HTTP call and keeps the source (or the transcode command, for `webrtc`) always current.
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.mediamtx_control_url, timeout=MEDIAMTX_TIMEOUT
        ) as client:
            response = await client.post(f"/v3/config/paths/replace/{path_name}", json=config)
    except httpx.HTTPError as exc:
        raise ApiError(
            status_code=502,
            code="media_backend_unavailable",
            message="Could not reach the live-view media server. Try again shortly.",
        ) from exc

    if response.status_code not in (200, 201):
        raise ApiError(
            status_code=502,
            code="media_backend_rejected_config",
            message="The live-view media server rejected this camera's stream configuration.",
            details={"mediamtx_status": response.status_code},
        )


@router.post("/api/v1/tenant/cameras/{camera_id}/live-session", response_model=LiveSessionOut)
async def start_live_session(
    camera_id: uuid.UUID,
    body: LiveSessionIn,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> LiveSessionOut:
    require_permission(context, "camera.view_live")
    camera = await load_camera(db, camera_id)

    if camera.status != "ready":
        raise ApiError(
            status_code=409,
            code="camera_not_ready",
            message=(
                f"This camera is '{camera.status}', not 'ready' - probe it successfully "
                "before it can be watched live."
            ),
        )

    if body.protocol == "hls":
        stream_path = camera.main_stream_path
    else:
        stream_path = camera.sub_stream_path
        if not stream_path:
            # Works, just costs ~7x more (mainstream transcode) - said out loud rather than
            # silently substituted, matching this codebase's own established convention.
            logger.warning(
                "live_session_no_substream_transcoding_mainstream_instead",
                extra={"camera_id": str(camera_id)},
            )
            stream_path = camera.main_stream_path

    if not camera.hostname or not stream_path:
        raise ApiError(
            status_code=422,
            code="camera_not_configured",
            message="This camera has no hostname or stream path set, so there is nothing to watch.",
        )

    try:
        rtsp_url = await resolve_camera_rtsp_url(
            settings, db,
            tenant_id=context.tenant_id, camera_id=camera_id,
            hostname=camera.hostname, port=camera.rtsp_port or 554, path=stream_path,
            username=camera.username, secret_purpose=CREDENTIAL_PURPOSE,
        )
    except BlockedAddressError as exc:
        raise ApiError(status_code=422, code="address_not_permitted", message=str(exc)) from exc
    except CredentialUnreadableError as exc:
        raise ApiError(status_code=500, code="credential_store_failed", message=str(exc)) from exc

    path_name = _mediamtx_path_name(camera_id, body.protocol)

    if body.protocol == "hls":
        await _put_mediamtx_path(
            settings, path_name=path_name,
            config={
                "source": rtsp_url,
                "sourceOnDemand": True,
                "sourceOnDemandCloseAfter": "30s",
            },
        )
        play_url = f"{settings.media_public_base_url}/media/hls/{path_name}/index.m3u8"
    else:
        # No `source` at all: this path only ever publishes when read. `runOnDemand` fires
        # when a reader connects and nobody is publishing yet (confirmed against MediaMTX's
        # real config reference, not assumed) - ffmpeg pulls the already-resolved,
        # credential-embedded substream URL, transcodes to H.264, and republishes over the
        # RTSP loopback MediaMTX exposes to itself. No audio (`-an`) - a deliberate
        # simplification for a security live-view, not a hard requirement; adding it back
        # is a separate, later change if it turns out to matter.
        ffmpeg_cmd = (
            "ffmpeg -rtsp_transport tcp -i " + shlex.quote(rtsp_url) + " "
            "-c:v libx264 -preset veryfast -tune zerolatency -an "
            "-f rtsp rtsp://localhost:8554/" + path_name
        )
        await _put_mediamtx_path(
            settings, path_name=path_name,
            config={
                "runOnDemand": ffmpeg_cmd,
                "runOnDemandRestart": True,
                "runOnDemandCloseAfter": "30s",
            },
        )
        play_url = f"{settings.media_public_base_url}/media/webrtc/{path_name}/whep"

    token = await create_media_session(
        request.app.state.redis, settings,
        tenant_id=context.tenant_id, camera_id=camera_id, path=path_name, protocol=body.protocol,
    )

    logger.info(
        "live_session_started",
        extra={
            "camera_id": str(camera_id), "protocol": body.protocol,
            "actor": str(context.user_id),
        },
    )

    return LiveSessionOut(
        path=path_name, protocol=body.protocol, token=token,
        play_url=f"{play_url}?token={token}", expires_in=MEDIA_SESSION_TTL_SECONDS,
    )


class MediaMtxAuthRequest(BaseModel):
    """The real, verified MediaMTX HTTP-auth-webhook payload shape - not guessed."""

    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str = ""
    query: str = ""
    userAgent: str = ""


def _token_from_query(raw_query: str) -> str:
    parsed = parse_qs(raw_query)
    values = parsed.get("token")
    return values[0] if values else ""


@router.post("/api/v1/tenant/media/authenticate", status_code=200)
async def authenticate_media_request(
    body: MediaMtxAuthRequest, request: Request, settings: Settings = Depends(get_app_settings)
) -> dict:
    """Called by MediaMTX itself, server-to-server, on every connection attempt - not by a
    browser, and carries no bearer token of its own. This endpoint **is** the auth check
    (see module docstring); MediaMTX only inspects the status code, any 20x authorizes.

    Only ever authorizes `read`: no session this service issues is ever minted for
    `publish`. The webrtc transcode's own loopback publish (its ffmpeg `runOnDemand`
    process, started from this same file) does **not** come through here at all - it is
    excluded at the MediaMTX config level instead (`mediamtx.yml`'s `authHTTPExclude`,
    scoped narrowly to `publish` on `*-webrtc` paths only). That exclusion exists because
    testing this directly found the opposite of what a first pass assumed: MediaMTX's
    `authMethod: http` gates *every* publish by default, including its own internal
    loopback one, and the ffmpeg command carries no credentials of its own to satisfy a
    webhook check here.
    """
    token = body.token or _token_from_query(body.query)
    if not token:
        raise ApiError(status_code=401, code="no_token", message="No session token supplied.")

    session = await check_media_session(request.app.state.redis, settings, token)
    if session is None:
        raise ApiError(
            status_code=401, code="invalid_or_expired_session",
            message="Invalid or expired session.",
        )

    if body.action != "read":
        raise ApiError(
            status_code=403, code="action_not_authorized",
            message="This session only authorizes reading.",
        )

    if body.path != session.path:
        raise ApiError(
            status_code=403, code="wrong_path",
            message="This session does not authorize this path.",
        )

    return {}
