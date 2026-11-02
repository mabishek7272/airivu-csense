"""Re-export shim. The real implementation moved to `csense_shared.cameras.probe` so
admin_api's tenant-infra-management endpoints (which manage cameras on a tenant's behalf)
can share it too - two independent copies of a security-sensitive, hand-rolled RTSP/
digest-auth exchange is exactly the kind of duplication that goes quietly wrong. Kept here,
re-exporting, rather than deleted outright, so every existing `from app.services.
camera_probe import ...` in this service (`cameras.py`, `nvr.py`) keeps working unchanged.
"""
from __future__ import annotations

from csense_shared.cameras.connection import resolve_camera_endpoint, tunnel_networks
from csense_shared.cameras.probe import (
    ProbeOutcome,
    camera_secret_query,
    device_tunnel_networks,
    probe_stream,
)

__all__ = [
    "ProbeOutcome",
    "camera_secret_query",
    "device_tunnel_networks",
    "probe_stream",
    "resolve_camera_endpoint",
    "tunnel_networks",
]
