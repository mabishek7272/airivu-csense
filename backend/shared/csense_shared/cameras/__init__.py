"""Camera-related logic shared across services - see `csense_shared.cameras.health` for
CHECKLIST's "Camera health use cases" classification."""
from __future__ import annotations

from csense_shared.cameras.health import (
    LOW_FRAMERATE_THRESHOLD_FPS,
    SLOW_PROBE_THRESHOLD_MS,
    classify_health_events,
)

__all__ = ["LOW_FRAMERATE_THRESHOLD_FPS", "SLOW_PROBE_THRESHOLD_MS", "classify_health_events"]
