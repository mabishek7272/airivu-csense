"""Guards on outbound connections to addresses a tenant supplies.

Camera probing takes a hostname from a customer and makes the server connect to it. That
is server-side request forgery by construction, and the only thing separating it from the
vulnerability is where the connection is allowed to land.

**Private and loopback addresses are refused.** The cloud has no legitimate reason to
reach its own private network on a tenant's instruction, and the addresses an attacker
wants are exactly the ones a naive implementation allows: `127.0.0.1` for the admin API,
`10.x`/`172.16.x`/`192.168.x` for Postgres, MinIO and Redis, `169.254.169.254` for the
cloud metadata service that hands out instance credentials.

Cameras on a customer's LAN are *not* an exception to this. The cloud cannot route to a
private address on someone else's network anyway - that is what the edge device exists
for, and the edge probes those cameras from inside the network where it belongs. So the
rule costs nothing real and closes the whole class.

**Resolution happens here, and the resolved address is what gets connected to.** Checking
the hostname and then handing the name to a connect call leaves a DNS rebinding window:
the name resolves to a public address for the check and a private one microseconds later.
Callers use `resolve_public_endpoint` and dial the returned IP.
"""
from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)


class BlockedAddressError(ValueError):
    """The requested address is not one the server will connect to."""


def _describe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why an address is refused, or None if it is acceptable.

    `is_global` is not used on its own: it is false for several ranges that deserve their
    own message, and an operator staring at "blocked" needs to know *which* rule caught
    their camera.
    """
    if address.is_loopback:
        return "a loopback address"
    if address.is_link_local:
        # 169.254.0.0/16 - the cloud metadata endpoint lives here.
        return "a link-local address"
    if address.is_private:
        return "a private address"
    if address.is_reserved:
        return "a reserved address"
    if address.is_multicast:
        return "a multicast address"
    if address.is_unspecified:
        return "an unspecified address"

    # IPv4-mapped and 6to4 IPv6 addresses can smuggle a private v4 address past a naive
    # v6 check, so unwrap and re-test rather than trusting the v6 properties alone.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            inner = _describe(address.ipv4_mapped)
            return f"{inner} in IPv4-mapped form" if inner else None
        if address.sixtofour is not None:
            inner = _describe(address.sixtofour)
            return f"{inner} in 6to4 form" if inner else None
    return None


def check_public_address(value: str) -> None:
    """Raises if a literal IP address is not one we will connect to."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BlockedAddressError(f"'{value}' is not a valid IP address.") from exc

    reason = _describe(address)
    if reason:
        raise BlockedAddressError(
            f"{value} is {reason} and cannot be reached from the cloud. Cameras on a "
            "private network are connected through an edge device, which probes them "
            "from inside that network."
        )


def resolve_public_endpoint(hostname: str, port: int) -> list[tuple[int, str]]:
    """Resolves a hostname and returns only the addresses we will connect to.

    Returns `(family, address)` pairs so the caller can dial the resolved IP directly.
    That matters: connecting by name would re-resolve, and a name that answered publicly
    during the check can answer privately a moment later.

    Every resolved address must pass. A name that returns one public and one private
    address is refused outright rather than filtered, because that pattern is far more
    likely to be a rebinding attempt than a misconfiguration.
    """
    if not hostname or len(hostname) > 253:
        raise BlockedAddressError("Hostname is empty or too long.")
    if not 1 <= port <= 65535:
        raise BlockedAddressError(f"Port {port} is out of range.")

    try:
        infos = socket.getaddrinfo(
            hostname, port, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise BlockedAddressError(f"'{hostname}' could not be resolved.") from exc

    if not infos:
        raise BlockedAddressError(f"'{hostname}' resolved to no addresses.")

    endpoints: list[tuple[int, str]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        reason = _describe(ipaddress.ip_address(ip))
        if reason:
            logger.warning(
                "outbound_address_blocked",
                extra={"hostname": hostname[:120], "reason": reason},
            )
            raise BlockedAddressError(
                f"'{hostname}' resolves to {ip}, which is {reason}. The cloud does not "
                "connect to private networks on request; use an edge device for cameras "
                "that are not publicly reachable."
            )
        endpoints.append((family, ip))

    return endpoints
