"""SSRF guard: which addresses the server will connect to on a tenant's instruction.

Camera probing is server-side request forgery by construction - a customer supplies a
hostname and the server connects to it. These tests pin down the boundary, because the
addresses an attacker wants are exactly the ones a permissive implementation allows:
loopback for the admin API, RFC1918 for Postgres and MinIO, and 169.254.169.254 for the
cloud metadata service that hands out instance credentials.

No network: hostname resolution is stubbed.
"""
from __future__ import annotations

import socket

import pytest

from csense_shared.security.outbound import (
    BlockedAddressError,
    check_public_address,
    resolve_public_endpoint,
)

# --- Literal addresses ----------------------------------------------------------------

@pytest.mark.parametrize(
    "address,why",
    [
        ("127.0.0.1", "loopback - the server's own APIs"),
        ("127.1.1.1", "the whole loopback /8, not just .0.1"),
        ("0.0.0.0", "unspecified"),
        ("10.0.0.5", "RFC1918"),
        ("172.16.0.5", "RFC1918"),
        ("172.31.255.254", "top of the RFC1918 /12"),
        ("192.168.1.10", "RFC1918"),
        ("169.254.169.254", "cloud metadata - instance credentials live here"),
        ("169.254.1.1", "link-local generally"),
        ("::1", "IPv6 loopback"),
        ("fe80::1", "IPv6 link-local"),
        ("fc00::1", "IPv6 unique-local"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
    ],
)
def test_internal_addresses_are_refused(address, why):
    with pytest.raises(BlockedAddressError):
        check_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",      # IPv4-mapped loopback
        "::ffff:10.0.0.1",       # IPv4-mapped RFC1918
        "::ffff:169.254.169.254",  # IPv4-mapped metadata endpoint
        "2002:a00:1::",          # 6to4 wrapping 10.0.0.1
        "2002:7f00:1::",         # 6to4 wrapping 127.0.0.1
    ],
)
def test_v4_addresses_smuggled_through_v6_are_refused(address):
    """A private v4 address wrapped in IPv6 passes a naive v6-only check."""
    with pytest.raises(BlockedAddressError):
        check_public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "206.148.37.112", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    check_public_address(address)


def test_the_reason_names_the_rule():
    """An operator whose camera is refused has to know which rule caught it."""
    with pytest.raises(BlockedAddressError, match="private"):
        check_public_address("192.168.1.10")
    with pytest.raises(BlockedAddressError, match="link-local"):
        check_public_address("169.254.169.254")
    with pytest.raises(BlockedAddressError, match="loopback"):
        check_public_address("127.0.0.1")


def test_garbage_is_refused():
    with pytest.raises(BlockedAddressError):
        check_public_address("not-an-address")
    with pytest.raises(BlockedAddressError):
        check_public_address("")


# --- Resolution -----------------------------------------------------------------------

def fake_getaddrinfo(addresses):
    def _resolve(host, port, **kwargs):
        if not addresses:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, port))
            for a in addresses
        ]

    return _resolve


def test_public_hostname_resolves_to_dialable_addresses(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(["206.148.37.112"]))

    endpoints = resolve_public_endpoint("nvr.example.com", 554)

    # The resolved IP is returned so the caller dials it directly rather than the name -
    # re-resolving at connect time is the rebinding window this closes.
    assert endpoints == [(socket.AF_INET, "206.148.37.112")]


def test_hostname_resolving_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(["10.0.0.5"]))

    with pytest.raises(BlockedAddressError, match="private"):
        resolve_public_endpoint("internal.example.com", 554)


def test_mixed_public_and_private_answers_are_refused(monkeypatch):
    """A name answering with both is far more likely rebinding than misconfiguration, so
    it is refused outright rather than filtered down to the public one."""
    monkeypatch.setattr(
        socket, "getaddrinfo", fake_getaddrinfo(["206.148.37.112", "127.0.0.1"])
    )

    with pytest.raises(BlockedAddressError):
        resolve_public_endpoint("rebind.example.com", 554)


def test_metadata_endpoint_via_dns_is_refused(monkeypatch):
    """The classic SSRF payload: a public name pointed at the metadata service."""
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(["169.254.169.254"]))

    with pytest.raises(BlockedAddressError, match="link-local"):
        resolve_public_endpoint("harmless-looking-name.example.com", 554)


def test_unresolvable_hostname_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo([]))

    with pytest.raises(BlockedAddressError, match="could not be resolved"):
        resolve_public_endpoint("nope.example.invalid", 554)


def test_input_bounds():
    with pytest.raises(BlockedAddressError):
        resolve_public_endpoint("", 554)
    with pytest.raises(BlockedAddressError):
        resolve_public_endpoint("x" * 300, 554)
    with pytest.raises(BlockedAddressError, match="out of range"):
        resolve_public_endpoint("nvr.example.com", 0)
    with pytest.raises(BlockedAddressError, match="out of range"):
        resolve_public_endpoint("nvr.example.com", 70000)
