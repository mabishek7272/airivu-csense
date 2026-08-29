"""NVR channel listing: the adapter interface, and the one mock reference adapter.

Different from ONVIF WS-Discovery (see `csense_shared.onvif.discovery`): listing an NVR's
channels is a direct, unicast call to a host the caller already named, not a multicast
broadcast bound to one network segment - the same shape as `camera_probe.py`'s own
DESCRIBE exchange, so it can run centrally today, including over a tenant's provisioned
VPN tunnel (`nvr.py` reuses the same SSRF/tunnel-awareness `camera_probe.py` already has).

`MockNVRAdapter` is deliberately not a real integration - it never opens a socket. Real
vendor NVRs speak wildly different channel-listing protocols (ONVIF Media/Device SOAP
services, or proprietary HTTP APIs depending on the vendor), and CHECKLIST's own scope for
this pass names exactly one adapter, and names it mock. `ADAPTERS_BY_KIND` exists so a real
adapter is a one-line registry addition later, not a rewrite of the callers - mirrors
`engines.py`'s `ENGINES_BY_RUNTIME` for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NVRChannel:
    """What FLOW-05 step 4 says a discovery result shows: "vendor, model, address,
    profiles" - plus the stream paths needed to prefill a real `CameraIn` once an operator
    picks a channel to onboard."""

    channel_id: str
    name: str
    main_stream_path: str
    sub_stream_path: str | None
    vendor: str | None
    model: str | None


class NVRAdapter(Protocol):
    async def list_channels(
        self, *, hostname: str, port: int, username: str, password: str
    ) -> list[NVRChannel]: ...


class MockNVRAdapter:
    """The one reference adapter this pass ships. Returns a small, deterministic, clearly
    fake channel list regardless of what it is asked to connect to - no socket is ever
    opened. Exists to prove out the interface, the API route, and the "turn a discovered
    channel into a real camera" flow end-to-end without needing real NVR hardware."""

    async def list_channels(
        self, *, hostname: str, port: int, username: str, password: str
    ) -> list[NVRChannel]:
        return [
            NVRChannel(
                channel_id=str(i),
                name=f"Channel {i}",
                main_stream_path=f"/cam/realmonitor?channel={i}&subtype=0",
                sub_stream_path=f"/cam/realmonitor?channel={i}&subtype=1",
                vendor="Mock NVR",
                model="MOCK-4CH",
            )
            for i in range(1, 5)
        ]


ADAPTERS_BY_KIND: dict[str, type] = {"mock": MockNVRAdapter}


def build_adapter(kind: str = "mock") -> NVRAdapter:
    adapter_cls = ADAPTERS_BY_KIND.get(kind)
    if adapter_cls is None:
        raise ValueError(f"No NVR adapter registered for kind '{kind}'")
    return adapter_cls()
