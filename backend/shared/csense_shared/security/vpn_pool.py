"""Allocates WireGuard tunnel addresses to edge devices from a managed pool.

The deployment guide's own template hardcodes 10.0.0.2 for the client, which collides on
the second site and caps the fleet at ~253 peers under a /24. This hands out a
globally-unique /32 from a much larger pool instead, so provisioning one device never
requires knowing what every other device in the fleet has already claimed - the database's
own uniqueness constraint on `vpn_address` is the backstop, not this module's bookkeeping.

This closes only one half of the flaw the guide's templates introduced. The other half -
every peer's `AllowedIPs = 10.0.0.0/24`, which lets one tenant's device route to another
tenant's cameras - is not an addressing problem, and a bigger pool does not fix it. A
larger pool that every peer is still allowed to route across is the same flaw with more
room to collide in. That half is closed by always rendering a peer's `AllowedIPs` as its
own /32 (see `edge.py`'s `_render_server_peer_block`, which is meant to be the only place a
WireGuard peer stanza is written from now on - not hand-copied from the guide).
"""
from __future__ import annotations

import ipaddress

# Two orders of magnitude past the guide's /24 (254 usable addresses), so running out is
# not something day-to-day operation runs into. RFC 1918, and deliberately not the same
# range as anything in `reserved_local_networks` (outbound.py) - a pool address must never
# be mistaken for one of the ranges this host already routes locally.
DEFAULT_POOL_CIDR = "10.8.0.0/16"


def next_free_address(
    pool: ipaddress.IPv4Network, used: set[str]
) -> ipaddress.IPv4Address | None:
    """The lowest unused host address in the pool, or None if it is exhausted.

    Lowest-first rather than random: a device's tunnel address is not a credential (its
    WireGuard public key is), so there is nothing to gain from unpredictability, and a
    deterministic order makes an allocation reproducible to read back and debug.
    """
    for candidate in pool.hosts():
        if str(candidate) not in used:
            return candidate
    return None
