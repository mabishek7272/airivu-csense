"""Camera health use-case classification (CHECKLIST: "Camera health use cases: offline,
obstruction, glare/night-vision, low FPS, network").

**Scoped to what a connectivity probe can actually see, named explicitly rather than
silently narrowed.** `offline` (no answer at all) and `low FPS` (the stream's own
reported frame rate) are directly measurable from a real RTSP probe
(`tenant_api/app/services/camera_probe.py`) and its SDP-parsed metrics. `network`
(answers, but slowly) is measurable from the probe's own round-trip time. `obstruction`
and `glare/night-vision` are **not** classified here: both need a decoded video frame to
analyze (brightness/variance statistics), and the connectivity probe deliberately speaks
RTSP directly rather than shelling out to ffmpeg - reversing that just for a health check
would be the wrong place to add a frame-decode dependency. A real snapshot-based health
check is a legitimate, separate feature (this deployment's own H.265-only NVR and IR
night imagery mean any glare/obstruction thresholds would need real validation data
before being trusted, the same caution already recorded for detection thresholds against
that same hardware) - not a stub built under time pressure.

A plain, dependency-free module on purpose: takes primitive values, not a probe-specific
result type, so it has no coupling to any one service's own dataclasses and is safely
importable (and unit-testable, no DB or network) from any service or test.
"""
from __future__ import annotations

# 5 fps is a common floor for a stream to still be reviewable as security footage - well
# below what a live-view feed would want, but a real signal that something (bandwidth,
# encoder load, a misconfigured substream) is degrading the stream rather than merely
# stylistic. 3 seconds is comfortably above real LAN/VPN round-trip time for an RTSP
# handshake, and comfortably below a probe's own connect/read timeouts (a probe that is
# THIS slow but still answers is the "network" case; one that never answers at all is
# "offline", not "network").
LOW_FRAMERATE_THRESHOLD_FPS = 5.0
SLOW_PROBE_THRESHOLD_MS = 3000


def classify_health_events(
    *, reachable: bool, detail: str, elapsed_ms: int | None, framerate: float | None,
) -> list[dict]:
    """Turns one probe outcome into the `camera_health_events` rows it should produce.
    One `connectivity` event always (the same online/offline signal this endpoint has
    always recorded); additional `network`/`framerate` events only when the probe's own
    measurements actually cross a threshold.
    """
    events = [{
        "status": "online" if reachable else "offline",
        "check_name": "connectivity", "detail": detail[:500],
    }]
    if reachable and elapsed_ms is not None and elapsed_ms > SLOW_PROBE_THRESHOLD_MS:
        events.append({
            "status": "degraded", "check_name": "network",
            "detail": f"Probe took {elapsed_ms}ms to answer (threshold {SLOW_PROBE_THRESHOLD_MS}ms).",
        })
    if reachable and framerate is not None and framerate < LOW_FRAMERATE_THRESHOLD_FPS:
        events.append({
            "status": "degraded", "check_name": "framerate",
            "detail": f"Stream reports {framerate:.1f}fps (threshold {LOW_FRAMERATE_THRESHOLD_FPS:.0f}fps).",
        })
    return events
