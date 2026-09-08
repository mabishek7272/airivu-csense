"""Resolving a camera's dial address before anything actually connects to it.

Two facts have to be combined before a single byte goes to a camera: what private-address
allowlist this tenant's own provisioning earns it (`tunnel_networks` - the peer's own VPN
address and the site LAN its tunnel routes, never "private addresses are fine"), and what
a hostname actually resolves to right now, checked against that allowlist and resolved
exactly once (`csense_shared.security.outbound.resolve_public_endpoint`).
`camera_probe.py`'s own `_probe_stream` established this pairing first, hand-in-hand with
its own DESCRIBE exchange; `resolve_camera_endpoint` is now the one place the pairing
itself lives, so a probe, a frame grab (`csense_shared.cameras.frame_grab`), or anything
else this platform later needs to dial a camera from gets the same DNS-rebinding-safe
behaviour rather than a second inline copy that could quietly drift from the first.

**Resolve once, dial the resolved address, never the hostname again** - see
`resolve_public_endpoint`'s own module docstring for why: a name that answered publicly
during the check can answer privately a moment later, and a media client (or an RTSP
decoder) that re-resolves the hostname itself would reopen exactly that window.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.security.outbound import parse_networks, resolve_public_endpoint

logger = logging.getLogger(__name__)


async def tunnel_networks(session: AsyncSession, *, camera_id: uuid.UUID) -> list:
    """The private ranges this camera is legitimately reachable on, if any.

    Derived entirely from what the tenant has provisioned: the edge device's own VPN
    address and the site LAN that device's tunnel routes. A camera in `direct` mode gets
    an empty list and is therefore held to the public-address rule, which is correct - a
    DDNS name has no business resolving to 10.x.

    Row-level security scopes this query, so one tenant's camera can never pick up
    another tenant's tunnel. The lookup joins through the camera rather than taking a
    device id from the caller, for the same reason.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT c.connection_mode, host(d.vpn_address), text(d.lan_cidr)
                FROM cameras c
                LEFT JOIN edge_devices d
                       ON d.id = c.edge_device_id AND d.deleted_at IS NULL
                WHERE c.id = :id
                """
            ),
            {"id": camera_id},
        )
    ).first()

    if row is None or row[0] not in ("vpn", "edge"):
        return []

    candidates = []
    if row[1]:
        # The peer itself, as a single address - never the /24 it sits in, which would
        # allowlist every other tenant's peer on the same interface.
        candidates.append(f"{row[1]}/32")
    if row[2]:
        candidates.append(row[2])

    if not candidates:
        logger.info(
            "camera_tunnel_has_no_provisioned_addresses",
            extra={"camera_id": str(camera_id)},
        )
    return parse_networks(candidates)


async def resolve_camera_endpoint(
    session: AsyncSession, *, camera_id: uuid.UUID, hostname: str, port: int,
) -> tuple[str, int]:
    """Returns `(resolved_ip, port)` - the address to actually dial, never the hostname
    again after this call. See `csense_shared.security.outbound`'s own module docstring
    for why: a name that answered publicly during the check can answer privately a moment
    later.

    Raises `BlockedAddressError` (from `csense_shared.security.outbound`) if the resolved
    address is not one this tenant has provisioned a legitimate reason to reach.
    """
    allowed = await tunnel_networks(session, camera_id=camera_id)
    endpoints = resolve_public_endpoint(hostname, port, allowed_networks=allowed)
    _family, address = endpoints[0]
    return address, port
