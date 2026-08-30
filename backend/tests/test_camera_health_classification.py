"""Camera health use-case classification (CHECKLIST: "Camera health use cases: offline,
obstruction, glare/night-vision, low FPS, network") - pure logic, no DB or real camera
needed. See `csense_shared.cameras.health`'s own docstring, and migration 0049 for why
obstruction/glare are not among these (no frame decode in this probe).
"""
from __future__ import annotations

from csense_shared.cameras.health import (
    LOW_FRAMERATE_THRESHOLD_FPS,
    SLOW_PROBE_THRESHOLD_MS,
    classify_health_events,
)


def test_a_healthy_probe_produces_one_connectivity_event_only():
    events = classify_health_events(reachable=True, detail="Connected.", elapsed_ms=100, framerate=25.0)
    assert events == [{"status": "online", "check_name": "connectivity", "detail": "Connected."}]


def test_an_unreachable_probe_is_offline_and_nothing_else():
    """Offline is the CHECKLIST use case that always applied - even before elapsed_ms or
    framerate existed to classify anything further."""
    events = classify_health_events(reachable=False, detail="No answer.", elapsed_ms=None, framerate=None)
    assert events == [{"status": "offline", "check_name": "connectivity", "detail": "No answer."}]


def test_a_slow_but_reachable_probe_adds_a_network_event():
    events = classify_health_events(
        reachable=True, detail="Connected.", elapsed_ms=SLOW_PROBE_THRESHOLD_MS + 1, framerate=25.0,
    )
    check_names = [e["check_name"] for e in events]
    assert check_names == ["connectivity", "network"]
    assert events[1]["status"] == "degraded"


def test_a_probe_exactly_at_the_slow_threshold_is_not_flagged():
    """The threshold names a real problem, not a coin flip at the boundary."""
    events = classify_health_events(
        reachable=True, detail="Connected.", elapsed_ms=SLOW_PROBE_THRESHOLD_MS, framerate=25.0,
    )
    assert [e["check_name"] for e in events] == ["connectivity"]


def test_a_low_framerate_probe_adds_a_framerate_event():
    events = classify_health_events(
        reachable=True, detail="Connected.", elapsed_ms=100, framerate=LOW_FRAMERATE_THRESHOLD_FPS - 0.1,
    )
    check_names = [e["check_name"] for e in events]
    assert check_names == ["connectivity", "framerate"]
    assert events[1]["status"] == "degraded"


def test_a_probe_exactly_at_the_framerate_threshold_is_not_flagged():
    events = classify_health_events(
        reachable=True, detail="Connected.", elapsed_ms=100, framerate=LOW_FRAMERATE_THRESHOLD_FPS,
    )
    assert [e["check_name"] for e in events] == ["connectivity"]


def test_both_network_and_framerate_can_fire_on_the_same_probe():
    events = classify_health_events(
        reachable=True, detail="Connected.",
        elapsed_ms=SLOW_PROBE_THRESHOLD_MS + 500, framerate=LOW_FRAMERATE_THRESHOLD_FPS - 1,
    )
    assert [e["check_name"] for e in events] == ["connectivity", "network", "framerate"]


def test_an_unreachable_probe_never_gets_network_or_framerate_events():
    """A camera that never answered has no framerate or round-trip time worth judging -
    those fields would be None anyway, but this also guards against a future caller
    passing stale, misleading values alongside reachable=False."""
    events = classify_health_events(
        reachable=False, detail="No answer.", elapsed_ms=SLOW_PROBE_THRESHOLD_MS + 1000, framerate=1.0,
    )
    assert [e["check_name"] for e in events] == ["connectivity"]


def test_a_reachable_probe_with_no_framerate_reported_is_not_flagged_low_fps():
    """Missing data is not the same as bad data - an SDP without an explicit framerate
    must not be misread as 0fps."""
    events = classify_health_events(reachable=True, detail="Connected.", elapsed_ms=100, framerate=None)
    assert [e["check_name"] for e in events] == ["connectivity"]
