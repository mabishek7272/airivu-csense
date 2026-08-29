"""VPN address allocation: the half of the WireGuard flaw that is about addressing.

The deployment guide's template hardcodes 10.0.0.2 for every client, so the second site
collides with the first. `next_free_address` is what replaces that: a deterministic,
collision-free pick from a much larger pool. It does not, on its own, stop one tenant's
peer routing to another's cameras - that is `_render_server_peer_block`'s job in edge.py,
covered by scripts/e2e_vpn_provisioning.py against the real server. This file is just the
allocator.
"""
from __future__ import annotations

import ipaddress

from csense_shared.security.vpn_pool import DEFAULT_POOL_CIDR, next_free_address


def test_empty_pool_hands_out_the_first_host_address():
    pool = ipaddress.ip_network("10.8.0.0/30")  # hosts: .1, .2
    assert str(next_free_address(pool, used=set())) == "10.8.0.1"


def test_skips_addresses_already_in_use():
    pool = ipaddress.ip_network("10.8.0.0/29")  # hosts: .1 .. .6
    used = {"10.8.0.1", "10.8.0.2", "10.8.0.3"}
    assert str(next_free_address(pool, used)) == "10.8.0.4"


def test_never_hands_out_network_or_broadcast_address():
    pool = ipaddress.ip_network("10.8.0.0/29")
    used = {str(a) for a in pool.hosts()}  # every usable host address taken
    assert next_free_address(pool, used) is None


def test_exhausted_pool_returns_none_rather_than_raising():
    pool = ipaddress.ip_network("10.8.0.0/30")  # hosts: .1, .2
    assert next_free_address(pool, used={"10.8.0.1", "10.8.0.2"}) is None


def test_allocation_is_deterministic_not_random():
    # A device's tunnel address is not a credential - its WireGuard public key is - so
    # there is no security reason for the pick to be unpredictable, and every reason for
    # it to be reproducible when someone is reading logs back.
    pool = ipaddress.ip_network("10.8.5.0/24")
    used = {"10.8.5.1", "10.8.5.2"}
    first = next_free_address(pool, used)
    second = next_free_address(pool, used)
    assert first == second == ipaddress.ip_address("10.8.5.3")


def test_default_pool_is_two_orders_of_magnitude_past_the_guides_24():
    # The guide's own template is a /24: 254 usable addresses, colliding on the second
    # site. The whole point of a managed pool is that running out is not something normal
    # operation runs into.
    pool = ipaddress.ip_network(DEFAULT_POOL_CIDR)
    assert pool.num_addresses - 2 > 254 * 100


def test_default_pool_does_not_overlap_the_hosts_reserved_local_networks():
    # A pool address must never be mistaken for one of the ranges outbound.py already
    # treats as "this host's own network" - if it did, a device's own tunnel address
    # would itself trip the SSRF guard's reserved-local check.
    from csense_shared.config import Settings

    reserved = [
        ipaddress.ip_network(cidr.strip())
        for cidr in Settings.model_fields["reserved_local_networks"].default.split(",")
    ]
    pool = ipaddress.ip_network(DEFAULT_POOL_CIDR)
    assert not any(pool.overlaps(r) for r in reserved)
