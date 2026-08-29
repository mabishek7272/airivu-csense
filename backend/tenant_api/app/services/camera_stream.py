"""Resolving a camera's real, reachable RTSP source URL for MediaMTX to pull from.

Shares `camera_probe.py`'s own SSRF guard and credential-decrypt path
(`tunnel_networks`, `camera_secret_query`, `read_secret`/`keyring_from_settings`) rather
than re-deriving either - `probe_stream` keeps its own hand-rolled DESCRIBE exchange
unchanged; only the "which address is this camera legitimately reachable on, and what's
its password" part is shared.

**Why the URL embeds a resolved IP literal, not the hostname, unlike `probe_stream`'s own
URI.** `probe_stream` resolves and dials the IP itself, in the same request, so the
resolve-to-dial window is microseconds - that is what closes its DNS-rebinding gap. Here,
the connection is made by a *different process* (MediaMTX), often much later
(`sourceOnDemand` doesn't pull until a viewer shows up) - if the URL carried the hostname,
MediaMTX would re-resolve it independently at pull time, and everything this module checked
at session-creation time would be checking a name that no longer has to mean what it meant
a minute ago. Handing MediaMTX the literal address this process already validated closes
that gap entirely: MediaMTX never performs its own DNS lookup for a literal IP.
"""
from __future__ import annotations

import socket
import uuid
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.camera_probe import camera_secret_query, tunnel_networks
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import resolve_public_endpoint
from csense_shared.security.secret_store import read_secret


class CredentialUnreadableError(RuntimeError):
    """The stored credential exists but could not be decrypted."""


async def resolve_camera_rtsp_url(
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
) -> str:
    """Returns `rtsp://[user:pass@]<resolved-ip>:<port><path>` - a URL MediaMTX can pull
    directly as a generic RTSP client. Raises `BlockedAddressError` (from
    `csense_shared.security.outbound`) for an address this deployment refuses to reach, and
    `CredentialUnreadableError` if a stored credential exists but won't decrypt.
    """
    allowed = await tunnel_networks(session, camera_id=camera_id)
    endpoints = resolve_public_endpoint(hostname, port, allowed_networks=allowed)
    family, address = endpoints[0]
    host_literal = f"[{address}]" if family == socket.AF_INET6 else str(address)

    password: str | None = None
    if username:
        secret_row = await session.execute(camera_secret_query(), {"id": camera_id})
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
                raise CredentialUnreadableError(
                    "The stored credential could not be decrypted. Set it again."
                ) from exc

    auth = ""
    if username and password:
        # RTSP userinfo is a URL component - a password containing `@`, `:` or `/` would
        # otherwise be parsed as part of the host or path.
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"

    return f"rtsp://{auth}{host_literal}:{port}{path}"
