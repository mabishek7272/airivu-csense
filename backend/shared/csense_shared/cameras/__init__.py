"""Camera-related logic shared across services - see `csense_shared.cameras.health` for
CHECKLIST's "Camera health use cases" classification and `csense_shared.cameras.connection`
for the resolve-then-dial sequence every camera-connecting caller shares."""
from __future__ import annotations

from csense_shared.cameras.connection import resolve_camera_endpoint, tunnel_networks
from csense_shared.cameras.health import (
    LOW_FRAMERATE_THRESHOLD_FPS,
    SLOW_PROBE_THRESHOLD_MS,
    classify_health_events,
)

__all__ = [
    "LOW_FRAMERATE_THRESHOLD_FPS",
    "SLOW_PROBE_THRESHOLD_MS",
    "classify_health_events",
    "resolve_camera_endpoint",
    "tunnel_networks",
]
