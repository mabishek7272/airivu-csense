"""Pinning an outbound request to the address the SSRF guard approved.

`test_outbound_guard.py` pins down *which* addresses pass. These pin down that the
approved address is the one actually dialled - the half of the contract
`csense_shared.security.outbound`'s docstring states and that a caller re-resolving the
hostname at connect time silently breaks. The failure being prevented is DNS rebinding: a
name that answers publicly for the check and privately for the connect.

The TLS half is asserted too, in the only way that is really convincing: a certificate
that does not match the hostname must still be rejected. Pinning to an IP is exactly the
change most likely to disable hostname verification by accident, and that would be a worse
bug than the one being fixed here.

No network except the one test marked as needing it, which is skipped without
`ALLOW_NETWORK_TESTS=1`.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import httpx
import pytest

from csense_shared.security import pinned_http
from csense_shared.security.outbound import BlockedAddressError
from csense_shared.security.pinned_http import (
    pin_url_to_addresses,
    post_pinned,
    resolve_pinned_endpoint,
)


def fake_getaddrinfo(addresses, family=socket.AF_INET):
    def _resolve(host, port, *args, **kwargs):
        if not addresses:
            raise socket.gaierror("no answer")
        return [
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, port)) for a in addresses
        ]

    return _resolve


def test_the_dialled_url_carries_the_ip_and_the_headers_carry_the_hostname():
    pinned = pin_url_to_addresses(
        "https://hooks.example.com/inbound?token=abc", [(socket.AF_INET, "203.0.113.10")]
    )

    assert urlparse(pinned.urls[0]).hostname == "203.0.113.10"
    assert urlparse(pinned.urls[0]).path == "/inbound"
    assert urlparse(pinned.urls[0]).query == "token=abc"
    # The hostname survives in the two places that make the request still mean what it
    # meant: the receiver's virtual-host routing, and the TLS identity we demand.
    assert pinned.headers["Host"] == "hooks.example.com"
    assert pinned.extensions["sni_hostname"] == "hooks.example.com"


def test_a_non_default_port_appears_in_the_host_header_and_a_default_one_does_not():
    default = pin_url_to_addresses("https://a.example.com/h", [(socket.AF_INET, "203.0.113.10")])
    explicit = pin_url_to_addresses(
        "https://a.example.com:8443/h", [(socket.AF_INET, "203.0.113.10")]
    )

    assert default.headers["Host"] == "a.example.com"
    assert explicit.headers["Host"] == "a.example.com:8443"
    assert urlparse(explicit.urls[0]).port == 8443


def test_an_ipv6_address_is_bracketed_so_the_url_still_parses():
    pinned = pin_url_to_addresses("https://v6.example.com/h", [(socket.AF_INET6, "2001:db8::1")])

    assert pinned.urls[0].startswith("https://[2001:db8::1]:443/")
    assert urlparse(pinned.urls[0]).hostname == "2001:db8::1"


def test_every_resolved_address_is_kept_as_a_candidate():
    """Letting httpx resolve gave happy-eyeballs over a name's whole address set for free.
    Pinning to only the first would quietly drop that, and one dead address in a
    round-robin rotation would start failing deliveries that used to succeed."""
    pinned = pin_url_to_addresses(
        "https://a.example.com/h",
        [(socket.AF_INET, "203.0.113.10"), (socket.AF_INET, "203.0.113.11"),
         (socket.AF_INET, "203.0.113.10")],
    )

    assert len(pinned.urls) == 2  # the duplicate is collapsed, the distinct pair is kept
    assert urlparse(pinned.urls[1]).hostname == "203.0.113.11"


@pytest.mark.asyncio
async def test_resolve_pinned_endpoint_refuses_what_the_guard_refuses(monkeypatch):
    """The pinning wrapper must not widen the guard by so much as one address."""
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(["10.0.0.5"]))

    with pytest.raises(BlockedAddressError, match="private"):
        await resolve_pinned_endpoint("https://internal.example.com/hook")


@pytest.mark.asyncio
async def test_resolve_pinned_endpoint_pins_to_the_address_the_guard_approved(monkeypatch):
    # A genuinely global address, not an RFC 5737 documentation one: the tests above never
    # reach the guard so 203.0.113.x is fine there, but this one does, and Python's
    # `ipaddress` classifies the documentation ranges as private - the guard would refuse
    # it for the wrong reason and the assertion would prove nothing.
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(["93.184.216.34"]))

    pinned = await resolve_pinned_endpoint("https://hooks.example.com/inbound")

    assert pinned.urls == ("https://93.184.216.34:443/inbound",)
    assert pinned.hostname == "hooks.example.com"


@pytest.mark.asyncio
async def test_an_unreachable_address_falls_through_to_the_next_candidate(monkeypatch):
    """Only ConnectError advances - an HTTP answer or a bad certificate is the endpoint's
    real verdict and must not be re-asked once per address."""
    pinned = pin_url_to_addresses(
        "https://a.example.com/h",
        [(socket.AF_INET, "203.0.113.10"), (socket.AF_INET, "203.0.113.11")],
    )
    tried: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        tried.append(str(request.url))
        if request.url.host == "203.0.113.10":
            raise httpx.ConnectError("network is unreachable", request=request)
        return httpx.Response(200)

    class _MockedClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(pinned_http.httpx, "AsyncClient", _MockedClient)

    response = await post_pinned(
        pinned, content=b"{}", headers={}, timeout=httpx.Timeout(5.0)
    )

    assert response.status_code == 200
    assert len(tried) == 2


@pytest.mark.skipif(
    os.environ.get("ALLOW_NETWORK_TESTS") != "1",
    reason="makes a real TLS handshake to httpbin.org - set ALLOW_NETWORK_TESTS=1",
)
@pytest.mark.asyncio
async def test_certificate_verification_still_rejects_a_hostname_mismatch():
    """The one that matters. Pinning to an IP is the change most likely to disable
    certificate hostname verification by accident, so this drives a real handshake against
    a real host while claiming to be someone else: the certificate must not verify."""
    infos = socket.getaddrinfo("httpbin.org", 443, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM)
    ip = next(i[4][0] for i in infos if i[0] == socket.AF_INET)

    honest = pin_url_to_addresses("https://httpbin.org/post", [(socket.AF_INET, ip)])
    response = await post_pinned(
        honest, content=b"{}", headers={"Content-Type": "application/json"},
        timeout=httpx.Timeout(20.0, connect=10.0),
    )
    assert response.status_code == 200

    # Same socket, same validated IP - only the identity we demand of the peer changes.
    lying = pin_url_to_addresses("https://not-httpbin.example.com/post", [(socket.AF_INET, ip)])
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await post_pinned(
            lying, content=b"{}", headers={}, timeout=httpx.Timeout(20.0, connect=10.0)
        )
