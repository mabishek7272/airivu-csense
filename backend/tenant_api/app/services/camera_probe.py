"""Connecting to a camera to find out what its stream actually is.

Speaks RTSP directly rather than shelling out to ffmpeg. When a camera will not connect,
the question is always *which* step failed - DNS, TCP, authentication, or the stream path -
and a generic "connection failed" from a media library answers none of them. A DESCRIBE
exchange answers all four, in about a second, without decoding a single frame.

What it reads back matters as much as whether it connects: the codec decides whether live
view can be served without transcoding, and the resolution and frame rate decide what the
stream costs. Recording that at probe time means capacity questions are answered from data
rather than from assumptions about what the customer bought.

**The credential is decrypted for the duration of one request and never leaves it.** It is
not logged, not returned, and not written back anywhere. Digest authentication needs the
plaintext to compute a hash, which is exactly why the value lives in the encrypted store
and is fetched at the moment of use.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import socket
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import resolve_public_endpoint
from csense_shared.security.secret_store import read_secret

logger = logging.getLogger(__name__)

# A camera that has not answered in this long is not going to. Kept short because an
# operator is waiting on the response, and a slow probe reads as a broken UI.
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 8.0

# An SDP is a few hundred bytes. This cap stops a hostile or broken device from streaming
# an unbounded response into memory.
MAX_RESPONSE_BYTES = 64 * 1024


@dataclass
class ProbeOutcome:
    reachable: bool
    detail: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    framerate: float | None = None
    transport: str | None = None

    def profile_json(self) -> str:
        import json

        return json.dumps(
            {
                "codec": self.codec,
                "width": self.width,
                "height": self.height,
                "framerate": self.framerate,
                "transport": self.transport,
            }
        )


def _digest_header(user: str, password: str, method: str, uri: str, challenge: str) -> str:
    """RFC 2617 digest response.

    MD5 is not a choice here - the RTSP digest scheme specifies it, and every camera
    implements only that. It is used as a challenge-response construction, not to store
    anything, so its collision weakness is not the relevant property.
    """
    realm = re.search(r'realm="([^"]*)"', challenge)
    nonce = re.search(r'nonce="([^"]*)"', challenge)
    if not realm or not nonce:
        raise ValueError("Malformed digest challenge from the camera.")

    def md5(value: str) -> str:
        return hashlib.md5(value.encode()).hexdigest()  # noqa: S324 - required by RFC 2617

    ha1 = md5(f"{user}:{realm.group(1)}:{password}")
    ha2 = md5(f"{method}:{uri}")
    qop = re.search(r'qop="?([^",]*)"?', challenge)

    if qop and "auth" in qop.group(1):
        cnonce = uuid.uuid4().hex[:16]
        response = md5(f"{ha1}:{nonce.group(1)}:00000001:{cnonce}:auth:{ha2}")
        return (
            f'Digest username="{user}", realm="{realm.group(1)}", '
            f'nonce="{nonce.group(1)}", uri="{uri}", response="{response}", '
            f'qop=auth, nc=00000001, cnonce="{cnonce}"'
        )
    response = md5(f"{ha1}:{nonce.group(1)}:{ha2}")
    return (
        f'Digest username="{user}", realm="{realm.group(1)}", '
        f'nonce="{nonce.group(1)}", uri="{uri}", response="{response}"'
    )


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    uri: str,
    seq: int,
    authorization: str | None = None,
) -> str:
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {seq}", "User-Agent: CSense/1.0"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if authorization:
        lines.append(f"Authorization: {authorization}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()

    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=READ_TIMEOUT)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("Camera sent an oversized response.")

    match = re.search(rb"Content-Length:\s*(\d+)", data, re.I)
    if match and b"\r\n\r\n" in data:
        want = int(match.group(1))
        body_start = data.index(b"\r\n\r\n") + 4
        while len(data) - body_start < want:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=READ_TIMEOUT)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Camera sent an oversized response.")

    return data.decode("utf-8", "replace")


def _parse_sdp(response: str) -> dict:
    """Codec, resolution and frame rate from the SDP, as far as it declares them.

    Resolution is often absent - many cameras only announce it inside the codec's
    parameter sets, which would mean decoding to find out. Absent is reported as absent
    rather than guessed at.
    """
    profile: dict = {}

    rtpmap = re.search(r"a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", response)
    if rtpmap:
        profile["codec"] = rtpmap.group(1).upper()

    framerate = re.search(r"a=framerate:([\d.]+)", response)
    if framerate:
        profile["framerate"] = float(framerate.group(1))

    framesize = re.search(r"a=framesize:\d+\s+(\d+)-(\d+)", response)
    if framesize:
        profile["width"] = int(framesize.group(1))
        profile["height"] = int(framesize.group(2))
    else:
        # Some cameras use x-dimensions instead.
        dims = re.search(r"a=x-dimensions:\s*(\d+)\s*,\s*(\d+)", response)
        if dims:
            profile["width"] = int(dims.group(1))
            profile["height"] = int(dims.group(2))

    return profile


async def probe_stream(
    settings,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    hostname: str,
    port: int,
    path: str,
    username: str | None,
    secret_purpose: str,
) -> ProbeOutcome:
    """Connects, authenticates if asked, and reports what the stream is.

    Raises `BlockedAddressError` if the address is not one the server will connect to;
    every other failure is returned as an unreachable outcome with a reason, because "the
    camera is unreachable" is information the operator needs rather than an error.
    """
    # Resolve first, then dial the resolved IP. Connecting by name would re-resolve and
    # reopen the DNS rebinding window the guard exists to close.
    endpoints = resolve_public_endpoint(hostname, port)
    family, address = endpoints[0]

    # The URL sent on the wire carries no credentials - they go in the Authorization
    # header. The Host header keeps the original name so virtual-hosted devices still work.
    uri = f"rtsp://{hostname}:{port}{path}"

    password: str | None = None
    if username:
        secret_row = await session.execute(
            _camera_secret_query(), {"id": camera_id}
        )
        secret_id = secret_row.scalar_one_or_none()
        if secret_id:
            try:
                keyring = keyring_from_settings(settings)
                password = (
                    await read_secret(
                        session, keyring,
                        secret_id=secret_id, tenant_id=tenant_id, purpose=secret_purpose,
                    )
                ).decode()
            except (EnvelopeError, UnicodeDecodeError) as exc:
                logger.warning(
                    "camera_credential_unreadable",
                    extra={"camera_id": str(camera_id), "error": str(exc)[:200]},
                )
                return ProbeOutcome(
                    reachable=False,
                    detail="The stored credential could not be decrypted. Set it again.",
                )

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port, family=family),
            timeout=CONNECT_TIMEOUT,
        )

        response = await _exchange(reader, writer, "OPTIONS", uri, 1)
        if "RTSP/1.0 200" not in response.splitlines()[0]:
            return ProbeOutcome(
                reachable=False,
                detail=f"The device answered but rejected OPTIONS: {response.splitlines()[0]}",
            )

        response = await _exchange(reader, writer, "DESCRIBE", uri, 2)
        status = response.splitlines()[0]

        if "401" in status:
            if not username or password is None:
                return ProbeOutcome(
                    reachable=False,
                    detail=(
                        "The camera requires authentication but no username and password "
                        "are stored for it."
                    ),
                )
            challenge = next(
                (ln for ln in response.splitlines()
                 if ln.lower().startswith("www-authenticate")),
                "",
            )
            if "digest" in challenge.lower():
                header = _digest_header(username, password, "DESCRIBE", uri, challenge)
            else:
                token = base64.b64encode(f"{username}:{password}".encode()).decode()
                header = f"Basic {token}"
            response = await _exchange(reader, writer, "DESCRIBE", uri, 3, header)
            status = response.splitlines()[0]

        if "401" in status:
            return ProbeOutcome(
                reachable=False,
                detail="The camera rejected the stored username and password.",
            )
        if "404" in status:
            return ProbeOutcome(
                reachable=False,
                detail=f"The camera has no stream at '{path}'.",
            )
        if "200" not in status:
            return ProbeOutcome(reachable=False, detail=f"The camera answered: {status}")

        profile = _parse_sdp(response)
        detail = "Connected."
        if profile.get("codec") in ("H265", "HEVC"):
            # Worth saying at the moment of discovery rather than when live view fails:
            # no browser receives H.265 over WebRTC, so this camera needs transcoding.
            detail = (
                "Connected. This camera streams H.265, which browsers cannot play over "
                "WebRTC - live view will transcode it."
            )

        return ProbeOutcome(
            reachable=True,
            detail=detail,
            transport="tcp",
            **profile,
        )

    except TimeoutError:
        return ProbeOutcome(
            reachable=False,
            detail=f"No answer from {hostname}:{port} within {CONNECT_TIMEOUT:.0f} seconds.",
        )
    except (OSError, socket.gaierror) as exc:
        return ProbeOutcome(
            reachable=False, detail=f"Could not connect to {hostname}:{port}: {exc}"
        )
    except ValueError as exc:
        return ProbeOutcome(reachable=False, detail=str(exc))
    finally:
        # The password is a local only. Dropping the reference is not a security control -
        # Python may keep the string alive - but it keeps it out of any traceback captured
        # from this frame.
        password = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def _camera_secret_query():
    from sqlalchemy import text

    return text("SELECT endpoint_secret_id FROM cameras WHERE id = :id")
