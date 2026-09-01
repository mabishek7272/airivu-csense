"""Guards on outbound connections to addresses a tenant supplies.

Camera probing takes a hostname from a customer and makes the server connect to it. That
is server-side request forgery by construction, and the only thing separating it from the
vulnerability is where the connection is allowed to land.

**Private, loopback and link-local addresses are refused by default.** The addresses an
attacker wants are exactly the ones a naive implementation allows: `127.0.0.1` for the
server's own APIs, `10.x`/`172.16.x`/`192.168.x` for Postgres, MinIO and Redis, and
`169.254.169.254` for the cloud metadata service that hands out instance credentials.

**But a WireGuard peer is a legitimate exception, and this originally got that wrong.**
The recommended production deployment puts the camera at `10.0.0.2:554`, reachable because
a tunnel makes it routable. A blanket refusal of private addresses blocks the primary path.
So callers may pass `allowed_networks` - the specific ranges a tenant has provisioned and
we have recorded, being the peer's own address and the site LAN its tunnel routes.

**The dangerous overlap is not the one that looks dangerous.** A tenant declaring
`169.254.0.0/16` is obvious. The subtle one is `172.18.0.0/16` - Docker's default bridge.
Traffic there never reaches their tunnel, because the host's own local route wins; it
reaches our Postgres. `validate_allowlist_candidate` therefore checks a proposed range
against what this host can already reach locally, which is configuration rather than a
fixed list, because it depends on how the host is networked.

**Resolution happens here, and the resolved address is what gets connected to.** Checking
the hostname and then handing the name to a connect call leaves a DNS rebinding window:
the name resolves to a public address for the check and a private one microseconds later.
Callers use `resolve_public_endpoint` and dial the returned IP. For HTTPS that is not
simply "swap the host for the IP" - the certificate must still be verified against the
real hostname - so `csense_shared.security.pinned_http` wraps this module for that case
rather than leaving each caller to get the TLS identity right on its own.
"""
from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Ranges that may never be allowlisted, whatever a tenant declares. Loopback and
# link-local are here because no tunnel legitimately carries them; the metadata endpoint
# is called out separately because it is the specific address an attacker is reaching for.
NEVER_ALLOWLISTABLE: tuple[str, ...] = (
    "127.0.0.0/8",
    "169.254.0.0/16",
    "::1/128",
    "fe80::/10",
    "0.0.0.0/8",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def parse_networks(values) -> list[IPNetwork]:
    """Parses CIDR strings, ignoring blanks. Raises on anything malformed."""
    networks: list[IPNetwork] = []
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise BlockedAddressError(f"'{text}' is not a valid CIDR range.") from exc
    return networks


def validate_allowlist_candidate(
    candidate: str, reserved: list[IPNetwork] | None = None
) -> IPNetwork:
    """Checks that a range a tenant wants to reach through a tunnel is safe to allow.

    Refuses two classes:

      Ranges that are never legitimate over a tunnel - loopback, link-local, multicast.

      Ranges overlapping anything this host can already reach locally. This is the one
      that matters and the one that is easy to miss: a tenant declaring Docker's default
      bridge would have us connect to our own database instead of their camera, because
      the local route wins over the tunnel route. The reserved list is passed in rather
      than hardcoded because it depends on the deployment's own networking.
    """
    try:
        network = ipaddress.ip_network(candidate.strip(), strict=False)
    except ValueError as exc:
        raise BlockedAddressError(f"'{candidate}' is not a valid CIDR range.") from exc

    if network.prefixlen == 0:
        raise BlockedAddressError(
            "A default route cannot be allowlisted; name the site's own subnet."
        )

    for banned in parse_networks(NEVER_ALLOWLISTABLE):
        if network.version == banned.version and network.overlaps(banned):
            raise BlockedAddressError(
                f"{network} overlaps {banned}, which is never routed over a tunnel."
            )

    for local in reserved or ():
        if network.version == local.version and network.overlaps(local):
            raise BlockedAddressError(
                f"{network} overlaps {local}, a network this server can already reach "
                "directly. Traffic to it would go to our own infrastructure rather than "
                "through your tunnel, so it cannot be allowlisted."
            )

    return network


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


def _permitted_by_allowlist(
    address: IPAddress, allowed: list[IPNetwork] | None
) -> bool:
    """Whether a provisioned tunnel makes this otherwise-refused address legitimate.

    Loopback, link-local and the other never-allowlistable ranges are checked first, so no
    allowlist entry can reach them even if one somehow got stored.
    """
    if not allowed:
        return False
    for banned in parse_networks(NEVER_ALLOWLISTABLE):
        if address.version == banned.version and address in banned:
            return False
    return any(
        address.version == network.version and address in network for network in allowed
    )


def check_public_address(
    value: str, allowed_networks: list[IPNetwork] | None = None
) -> None:
    """Raises if an address is not one we will connect to.

    `allowed_networks` carries the ranges a tenant has provisioned through a tunnel - the
    peer's own address and the site LAN it routes. Without it, every private address is
    refused, which is right for a camera reached over the public internet and wrong for
    one reached over WireGuard.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BlockedAddressError(f"'{value}' is not a valid IP address.") from exc

    if _permitted_by_allowlist(address, allowed_networks):
        return

    reason = _describe(address)
    if reason:
        raise BlockedAddressError(
            f"{value} is {reason} and is not in any network this tenant has provisioned. "
            "A camera on a private network is reached through an edge device, and its "
            "address has to fall inside that device's tunnel."
        )


def resolve_public_endpoint(
    hostname: str, port: int, allowed_networks: list[IPNetwork] | None = None
) -> list[tuple[int, str]]:
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
        address = ipaddress.ip_address(ip)

        if _permitted_by_allowlist(address, allowed_networks):
            endpoints.append((family, ip))
            continue

        reason = _describe(address)
        if reason:
            logger.warning(
                "outbound_address_blocked",
                extra={"hostname": hostname[:120], "reason": reason},
            )
            raise BlockedAddressError(
                f"'{hostname}' resolves to {ip}, which is {reason} and is not in any "
                "network this tenant has provisioned. A camera on a private network is "
                "reached through an edge device, and its address has to fall inside that "
                "device's tunnel."
            )
        endpoints.append((family, ip))

    return endpoints
