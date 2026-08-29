"""WS-Discovery probe construction and ProbeMatch parsing.

Not a test of real network discovery (see discovery.py's own docstring for why this
module is never called from a live route) - a test that the two pure functions this
module actually exposes for that future edge-agent use are correct against real WS-
Discovery/ONVIF XML shapes, since neither has ever been run against a live device here.
"""
from __future__ import annotations

from csense_shared.onvif.discovery import (
    ProbeMatch,
    build_probe_message,
    parse_probe_matches,
)

# A real-shaped ONVIF ProbeMatches response (constructed from the WS-Discovery/ONVIF
# spec's own message shapes, not guessed at) - one camera, one NVR with two XAddrs.
_SAMPLE_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
    <wsa:MessageID>urn:uuid:aaaaaaaa-0000-0000-0000-000000000001</wsa:MessageID>
    <wsa:RelatesTo>urn:uuid:bbbbbbbb-0000-0000-0000-000000000001</wsa:RelatesTo>
  </soap:Header>
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:cccccccc-0000-0000-0000-000000000001</wsa:Address>
        </wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Dome-Cam-1</wsd:Scopes>
        <wsd:XAddrs>http://192.168.1.50/onvif/device_service</wsd:XAddrs>
        <wsd:MetadataVersion>1</wsd:MetadataVersion>
      </wsd:ProbeMatch>
      <wsd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:dddddddd-0000-0000-0000-000000000002</wsa:Address>
        </wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>onvif://www.onvif.org/name/NVR-16CH</wsd:Scopes>
        <wsd:XAddrs>http://192.168.1.51/onvif/device_service http://[fe80::1]/onvif/device_service</wsd:XAddrs>
        <wsd:MetadataVersion>3</wsd:MetadataVersion>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""


def test_probe_message_carries_the_required_ws_discovery_fields():
    message = build_probe_message(message_id="urn:uuid:test-fixed-id")

    assert b"http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe" in message
    assert b"urn:uuid:test-fixed-id" in message
    assert b"dn:NetworkVideoTransmitter" in message


def test_probe_message_mints_a_fresh_message_id_by_default():
    """WS-Addressing requires a probe's MessageID to be unique - two probes reusing one
    would be indistinguishable to a responder correlating replies via RelatesTo."""
    first = build_probe_message()
    second = build_probe_message()
    assert first != second


def test_probe_matches_parses_both_devices_from_a_real_shaped_response():
    matches = parse_probe_matches(_SAMPLE_RESPONSE)

    assert len(matches) == 2
    camera, nvr = matches

    assert camera.endpoint_address == "urn:uuid:cccccccc-0000-0000-0000-000000000001"
    assert camera.types == ["dn:NetworkVideoTransmitter"]
    assert "onvif://www.onvif.org/name/Dome-Cam-1" in camera.scopes
    assert camera.xaddrs == ["http://192.168.1.50/onvif/device_service"]

    assert nvr.endpoint_address == "urn:uuid:dddddddd-0000-0000-0000-000000000002"
    assert len(nvr.xaddrs) == 2
    assert "http://192.168.1.51/onvif/device_service" in nvr.xaddrs


def test_malformed_response_is_a_valid_empty_result_not_an_error():
    assert parse_probe_matches(b"not xml at all") == []
    assert parse_probe_matches(b"") == []


def test_well_formed_xml_with_no_probematches_is_a_valid_empty_result():
    """A genuinely empty discovery pass (nothing on the network answered) must not be
    indistinguishable from a parse failure - both return [], but for different, both
    legitimate, reasons."""
    empty = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Body/>
</soap:Envelope>"""
    assert parse_probe_matches(empty) == []


def test_a_probematch_with_neither_address_nor_xaddrs_is_dropped():
    """Nothing usable came out of it - not returned as a device with no way to ever be
    reached again."""
    useless = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
    assert parse_probe_matches(useless) == []


def test_probematch_equality_is_by_value():
    """Two matches naming the same device compare equal - dedup (the same responder
    answering more than once, which real WS-Discovery does) relies on this."""
    a = ProbeMatch(endpoint_address="x", types=[], scopes=[], xaddrs=["http://y"])
    b = ProbeMatch(endpoint_address="x", types=[], scopes=[], xaddrs=["http://y"])
    assert a == b
