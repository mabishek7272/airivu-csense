"""Outbound HTTPS that actually connects to the address the SSRF guard approved.

`csense_shared.security.outbound` states the contract this module exists to honour:
*"Resolution happens here, and the resolved address is what gets connected to. Checking
the hostname and then handing the name to a connect call leaves a DNS rebinding window."*
Every caller that resolves and then hands the original URL to `httpx` breaks that contract
in the most literal way possible - httpx re-resolves the name itself, so the address that
passed the check and the address the socket lands on are two different lookups, and an
attacker controlling the DNS record only has to answer publicly once and privately once.
The window is not theoretical; it is the standard way this guard is defeated.

So the request is aimed at the **IP**, and the hostname is carried separately:

  * the URL's host is replaced by the validated IP literal, so no name is ever resolved
    again between the check and the socket;
  * `Host:` carries the original hostname, so the receiver's virtual hosting still routes
    the request to the right site;
  * the `sni_hostname` request extension carries it too, which is the part that must not
    be got wrong: it sets `server_hostname` on the TLS handshake, and `server_hostname` is
    what Python's SSL layer checks the presented certificate against. Pin the IP without
    it and the handshake fails with "certificate is not valid for <ip>"; pin it and
    verification is exactly as strict as it was before - a certificate that does not match
    the configured hostname still raises `CERTIFICATE_VERIFY_FAILED`. The IP is only where
    we dial; the hostname is still who we require the peer to prove it is.

**Every validated address is tried, not just the first.** Letting httpx resolve gave us
its happy-eyeballs walk over a name's whole address set for free, and pinning to
`endpoints[0]` alone would quietly give that up: one unreachable address in a round-robin
rotation (or an AAAA record on a host with no IPv6 route) would fail a delivery that used
to succeed. Only `ConnectError` moves on to the next candidate - a refused or unroutable
address is worth another try, while an HTTP-level answer, a timeout, or a certificate that
does not match is the endpoint's real verdict and must not be re-asked eight times.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from csense_shared.security.outbound import BlockedAddressError, IPNetwork, resolve_public_endpoint

DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class PinnedEndpoint:
    """One request, aimed at validated IPs, still speaking for its real hostname.

    `urls` is the same request rewritten once per validated address, in the order
    resolution returned them; `headers` and `extensions` are what keep the receiver and
    the TLS handshake seeing the hostname rather than the IP.
    """

    hostname: str
    port: int
    urls: tuple[str, ...]
    headers: dict[str, str]
    extensions: dict[str, str]

    @property
    def display(self) -> str:
        """How to name this destination in a log or a failure summary - the hostname, not
        the IP, because that is what the operator configured and would recognise."""
        return f"{self.hostname}:{self.port}"


def pin_url_to_addresses(url: str, endpoints: list[tuple[int, str]]) -> PinnedEndpoint:
    """Rewrites `url` to dial each already-validated `(family, address)` pair directly.

    Takes the addresses rather than resolving, so the caller keeps the resolution - and
    therefore the SSRF verdict - in its own hands, and nothing here can widen it.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("URL has no hostname to pin.")
    port = parsed.port or DEFAULT_PORTS.get(parsed.scheme, 443)

    seen: set[str] = set()
    urls: list[str] = []
    for family, address in endpoints:
        if address in seen:
            continue
        seen.add(address)
        literal = f"[{address}]" if family == socket.AF_INET6 else address
        urls.append(urlunparse(parsed._replace(netloc=f"{literal}:{port}")))
    if not urls:
        raise BlockedAddressError(f"'{parsed.hostname}' resolved to no usable addresses.")

    # The port belongs in Host only when it is not the scheme's default, matching what any
    # client would have sent had it dialled the name itself - some receivers compare Host
    # against their configured canonical name and a gratuitous ":443" fails that check.
    host_header = parsed.hostname if port == DEFAULT_PORTS.get(parsed.scheme) else f"{parsed.hostname}:{port}"
    return PinnedEndpoint(
        hostname=parsed.hostname,
        port=port,
        urls=tuple(urls),
        headers={"Host": host_header},
        extensions={"sni_hostname": parsed.hostname},
    )


async def resolve_pinned_endpoint(
    url: str, *, allowed_networks: list[IPNetwork] | None = None
) -> PinnedEndpoint:
    """Resolves and validates `url`'s hostname, returning something that can only be sent
    to an address that passed. Raises `BlockedAddressError` (or `ValueError` for a URL with
    no hostname) exactly as `resolve_public_endpoint` does.

    The resolution runs in a thread because `socket.getaddrinfo` is blocking and has no
    timeout of its own. That matters most in the notification worker, where the webhook
    loop and the alert-dispatch loop share one event loop (`asyncio.gather` in
    `notification_worker/app/main.py`): a single tenant's webhook pointed at a hostname
    with an unresponsive DNS server would otherwise stall life-safety alert delivery for
    every tenant on that replica until the resolver gave up. It is the same reason
    `notification_worker/app/worker.py` puts its blocking MinIO calls on a thread.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("URL has no hostname.")
    port = parsed.port or DEFAULT_PORTS.get(parsed.scheme, 443)
    endpoints = await asyncio.to_thread(
        resolve_public_endpoint, parsed.hostname, port, allowed_networks
    )
    return pin_url_to_addresses(url, endpoints)


async def post_pinned(
    pinned: PinnedEndpoint,
    *,
    content: bytes,
    headers: dict[str, str],
    timeout: httpx.Timeout,
) -> httpx.Response:
    """POSTs `content` to the validated address, presenting `pinned.hostname` to both the
    receiver and the TLS handshake. See this module's docstring for why only `ConnectError`
    advances to the next candidate address."""
    last_error: httpx.ConnectError | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url in pinned.urls:
            try:
                return await client.post(
                    url,
                    content=content,
                    headers={**headers, **pinned.headers},
                    extensions=pinned.extensions,
                )
            except httpx.ConnectError as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise httpx.ConnectError(f"No address could be dialled for {pinned.display}.")
