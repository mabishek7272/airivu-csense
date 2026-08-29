"""WS-Discovery (ONVIF Core Spec §7): probe construction and ProbeMatch parsing.

**Not called from any Tenant API route, deliberately.** WS-Discovery is UDP multicast to
`239.255.255.250:3702` (IPv4) / `[ff02::c]:3702` (IPv6), scoped to one network segment by
the multicast protocol itself - it cannot cross the internet from a customer's LAN to this
cloud service, the same way a browser cannot multicast to a server it isn't on the same
subnet as. FLOW-05 says discovery is an edge responsibility for exactly this reason
(`docs/03_APPLICATION_FLOWS.md`: *"Edge performs bounded ONVIF discovery on approved
interfaces/subnets"*). Run from Tenant API, `probe_network()` below would only ever
discover devices on this container's own Docker network - worthless for a real customer,
and actively misleading if it ever silently returned something that looked like a real
result. `edge/agent/` is empty (Phase 3+, not yet implemented); this module is the
reference implementation waiting for it - built and tested now against real WS-Discovery
XML shapes, so it is not unverified code someone trusts later without ever having run it.

Everything here is stdlib (`xml.etree.ElementTree`, `socket`) - no SOAP/ONVIF library
dependency for a message shape this small and this stable (the WS-Discovery/ONVIF
namespaces have not changed since the spec was finalized).
"""
from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass
from xml.etree import ElementTree as ET

MULTICAST_ADDRESS = "239.255.255.250"
MULTICAST_PORT = 3702

_NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsd": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
}
for _prefix, _uri in _NS.items():
    ET.register_namespace(_prefix, _uri)


def build_probe_message(message_id: str | None = None) -> bytes:
    """The WS-Discovery Probe, scoped to ONVIF network video transmitters (cameras/NVRs) -
    `dn:NetworkVideoTransmitter` is the ONVIF device type both cameras and NVR channels
    advertise. `message_id` is injectable for tests that need to assert on it; a real probe
    always mints a fresh one (WS-Addressing requires a probe's MessageID to be unique).
    """
    mid = message_id or f"urn:uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{_NS['soap']}" xmlns:wsa="{_NS['wsa']}" xmlns:wsd="{_NS['wsd']}" xmlns:dn="{_NS['dn']}">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>{mid}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>"""
    return xml.encode("utf-8")


@dataclass(frozen=True)
class ProbeMatch:
    """One discovered device. `xaddrs` are the device's ONVIF service URLs (where the
    edge agent would send GetDeviceInformation/GetProfiles/GetStreamUri next) - usually
    one, sometimes more for a multi-homed device."""

    endpoint_address: str
    types: list[str]
    scopes: list[str]
    xaddrs: list[str]


def parse_probe_matches(response: bytes) -> list[ProbeMatch]:
    """Parses a WS-Discovery ProbeMatches response into `ProbeMatch` records.

    Malformed or empty input returns `[]` rather than raising - the same "an empty/absent
    result is valid, not an error" discipline `OnnxEngine`/`decode_raw_yolo` already use
    elsewhere in this codebase. A genuinely malformed multicast response is unremarkable
    (foreign traffic on the same multicast group, a truncated UDP datagram) and must not
    take down whatever is collecting responses.
    """
    try:
        root = ET.fromstring(response)
    except ET.ParseError:
        return []

    matches = root.findall(".//wsd:ProbeMatches/wsd:ProbeMatch", _NS)
    results = []
    for match in matches:
        address_el = match.find("wsa:EndpointReference/wsa:Address", _NS)
        address = address_el.text.strip() if address_el is not None and address_el.text else ""

        types_el = match.find("wsd:Types", _NS)
        types = (types_el.text or "").split() if types_el is not None else []

        scopes_el = match.find("wsd:Scopes", _NS)
        scopes = (scopes_el.text or "").split() if scopes_el is not None else []

        xaddrs_el = match.find("wsd:XAddrs", _NS)
        xaddrs = (xaddrs_el.text or "").split() if xaddrs_el is not None else []

        if not address and not xaddrs:
            # Neither a stable identity nor a reachable address - nothing usable came out
            # of this ProbeMatch, so it is dropped rather than returned as a device with
            # no way to ever be reached again.
            continue

        results.append(
            ProbeMatch(endpoint_address=address, types=types, scopes=scopes, xaddrs=xaddrs)
        )
    return results


def probe_network(*, timeout: float = 3.0, bind_interface: str = "") -> list[ProbeMatch]:
    """Sends a real WS-Discovery Probe over UDP multicast and collects ProbeMatch
    responses for `timeout` seconds. Real network I/O - meant for the edge agent, running
    on a customer's own network segment, not for this service (see module docstring).

    `bind_interface`: the local address of the NIC to probe from, when a host has more
    than one - left blank binds the OS default, which is wrong on a multi-homed edge
    device and is exactly the kind of thing FLOW-05's "approved interfaces/subnets"
    phrasing is about; the edge agent is expected to pass this explicitly once it exists.
    """
    message = build_probe_message()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    if bind_interface:
        sock.bind((bind_interface, 0))
    sock.settimeout(timeout)

    results: list[ProbeMatch] = []
    try:
        sock.sendto(message, (MULTICAST_ADDRESS, MULTICAST_PORT))
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
            except TimeoutError:
                break
            results.extend(parse_probe_matches(data))
    finally:
        sock.close()
    return results
