"""Rule evaluation: the logic that decides whether a detection becomes an incident.

These are the thresholds operators tune, and the place where false-positive fatigue is
won or lost, so the edge cases are pinned explicitly rather than left to inspection.
"""
from __future__ import annotations

import datetime as dt

import pytest

from csense_shared.pipeline.rules import (
    DetectedObject,
    RejectionReason,
    Rule,
    evaluate,
    is_within_cooldown,
    roi_overlap_fraction,
    within_active_hours,
)

NOON = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)
CENTRE_SQUARE = ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))


def obj(class_name="person", confidence=0.9, bbox=(0.4, 0.4, 0.6, 0.6)):
    return DetectedObject(class_name=class_name, confidence=confidence, bbox=bbox)


# --- ROI geometry --------------------------------------------------------------------

def test_box_fully_inside_roi_overlaps_completely():
    assert roi_overlap_fraction((0.4, 0.4, 0.6, 0.6), CENTRE_SQUARE) == 1.0


def test_box_fully_outside_roi_has_no_overlap():
    assert roi_overlap_fraction((0.85, 0.85, 0.95, 0.95), CENTRE_SQUARE) == 0.0


def test_partial_overlap_is_measured_by_area_not_centre_point():
    """A box straddling the boundary must report the real fraction. Centre-point
    containment would call this either 0 or 1, and an intruder standing on the edge of a
    restricted zone is exactly where that error matters."""
    # Box spans x 0.1..0.3; the ROI starts at x=0.2, so exactly half is inside.
    assert roi_overlap_fraction((0.1, 0.4, 0.3, 0.6), CENTRE_SQUARE) == pytest.approx(0.5, abs=1e-6)


def test_absent_roi_means_whole_frame():
    assert roi_overlap_fraction((0.0, 0.0, 1.0, 1.0), None) == 1.0


def test_degenerate_box_does_not_divide_by_zero():
    assert roi_overlap_fraction((0.5, 0.5, 0.5, 0.5), CENTRE_SQUARE) == 0.0


def test_polygon_winding_order_does_not_matter():
    """Zone polygons come from a UI where a user may draw either direction."""
    reversed_square = tuple(reversed(CENTRE_SQUARE))
    assert roi_overlap_fraction((0.4, 0.4, 0.6, 0.6), reversed_square) == 1.0


# --- Class filtering -----------------------------------------------------------------

def test_compliant_classes_do_not_alert():
    """The kitchen-safety model emits compliant states alongside violations. Alerting on
    `maskon` would raise an incident for every correctly-dressed worker."""
    rule = Rule(type_code="kitchen.ppe", alertable_classes=frozenset({"no_glove", "maskoff", "no_hairnet"}))
    outcome = evaluate(rule, [obj(class_name="maskon"), obj(class_name="glove")], captured_at=NOON)

    assert not outcome.fired
    assert {reason for _, reason in outcome.rejected} == {RejectionReason.CLASS_NOT_ALERTABLE}


def test_violation_class_alerts():
    rule = Rule(type_code="kitchen.ppe", alertable_classes=frozenset({"no_glove"}))
    outcome = evaluate(rule, [obj(class_name="no_glove")], captured_at=NOON)
    assert outcome.fired
    assert outcome.best.class_name == "no_glove"


# --- Confidence ----------------------------------------------------------------------

def test_low_confidence_is_rejected():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), min_confidence=0.7)
    outcome = evaluate(rule, [obj(confidence=0.5)], captured_at=NOON)
    assert not outcome.fired
    assert outcome.rejected[0][1] is RejectionReason.BELOW_CONFIDENCE


def test_confidence_boundary_is_inclusive():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), min_confidence=0.7)
    assert evaluate(rule, [obj(confidence=0.7)], captured_at=NOON).fired


# --- ROI gating ----------------------------------------------------------------------

def test_person_outside_restricted_zone_does_not_alert():
    """The whole point of a restricted-area rule: someone in the corridor is not an
    intrusion."""
    rule = Rule(
        type_code="zone.intrusion",
        alertable_classes=frozenset({"person"}),
        roi_polygon=CENTRE_SQUARE,
        min_roi_overlap=0.3,
    )
    outcome = evaluate(rule, [obj(bbox=(0.85, 0.85, 0.95, 0.95))], captured_at=NOON)
    assert not outcome.fired
    assert outcome.rejected[0][1] is RejectionReason.OUTSIDE_ROI


def test_person_inside_restricted_zone_alerts():
    rule = Rule(
        type_code="zone.intrusion",
        alertable_classes=frozenset({"person"}),
        roi_polygon=CENTRE_SQUARE,
        min_roi_overlap=0.3,
    )
    assert evaluate(rule, [obj(bbox=(0.4, 0.4, 0.6, 0.6))], captured_at=NOON).fired


def test_partial_entry_respects_the_overlap_threshold():
    """Half in, half out: alerts at a 0.3 threshold, not at 0.7."""
    lenient = Rule(
        type_code="zone.intrusion", alertable_classes=frozenset({"person"}),
        roi_polygon=CENTRE_SQUARE, min_roi_overlap=0.3,
    )
    strict = Rule(
        type_code="zone.intrusion", alertable_classes=frozenset({"person"}),
        roi_polygon=CENTRE_SQUARE, min_roi_overlap=0.7,
    )
    straddling = [obj(bbox=(0.1, 0.4, 0.3, 0.6))]

    assert evaluate(lenient, straddling, captured_at=NOON).fired
    assert not evaluate(strict, straddling, captured_at=NOON).fired


# --- Duration ------------------------------------------------------------------------

def test_single_frame_rejected_when_duration_required():
    """Requiring persistence is the main defence against single-frame flicker."""
    rule = Rule(
        type_code="zone.loitering", alertable_classes=frozenset({"person"}), min_consecutive_frames=5
    )
    outcome = evaluate(rule, [obj()], captured_at=NOON, consecutive_frames=1)
    assert not outcome.fired
    assert outcome.rejected[0][1] is RejectionReason.INSUFFICIENT_DURATION


def test_sustained_presence_alerts():
    rule = Rule(
        type_code="zone.loitering", alertable_classes=frozenset({"person"}), min_consecutive_frames=5
    )
    assert evaluate(rule, [obj()], captured_at=NOON, consecutive_frames=5).fired


# --- Schedules -----------------------------------------------------------------------

def test_overnight_window_wraps_past_midnight():
    """(22, 6) must mean 22:00-06:00, not the empty literal range 22..6. Reading it
    literally would disable the rule for its entire intended window."""
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), active_hours=(22, 6))

    assert within_active_hours(rule, NOON.replace(hour=23))
    assert within_active_hours(rule, NOON.replace(hour=2))
    assert not within_active_hours(rule, NOON.replace(hour=12))


def test_daytime_window_does_not_wrap():
    rule = Rule(type_code="ppe", alertable_classes=frozenset({"no_glove"}), active_hours=(9, 17))
    assert within_active_hours(rule, NOON.replace(hour=10))
    assert not within_active_hours(rule, NOON.replace(hour=20))


def test_detections_outside_schedule_are_rejected():
    rule = Rule(
        type_code="zone.intrusion", alertable_classes=frozenset({"person"}), active_hours=(22, 6)
    )
    outcome = evaluate(rule, [obj()], captured_at=NOON.replace(hour=12))
    assert not outcome.fired
    assert outcome.rejected[0][1] is RejectionReason.OUTSIDE_SCHEDULE


# --- Cooldown and correlation --------------------------------------------------------

def test_cooldown_suppresses_a_repeat():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), cooldown_seconds=300)
    assert is_within_cooldown(rule, NOON, NOON + dt.timedelta(seconds=60))


def test_cooldown_expires():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), cooldown_seconds=300)
    assert not is_within_cooldown(rule, NOON, NOON + dt.timedelta(seconds=301))


def test_first_ever_incident_is_never_in_cooldown():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}), cooldown_seconds=300)
    assert not is_within_cooldown(rule, None, NOON)


def test_correlation_key_ignores_track_id():
    """A person who leaves and re-enters gets a new track but is the same situation.
    Including track_id here would re-alert on every re-acquisition."""
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}))
    first = rule.correlation_key("cam-1", "person", "zone-a")
    second = rule.correlation_key("cam-1", "person", "zone-a")
    assert first == second


def test_correlation_key_separates_cameras_and_zones():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}))
    assert rule.correlation_key("cam-1", "person", "zone-a") != rule.correlation_key("cam-2", "person", "zone-a")
    assert rule.correlation_key("cam-1", "person", "zone-a") != rule.correlation_key("cam-1", "person", "zone-b")


# --- Rejection accounting ------------------------------------------------------------

def test_rejections_are_reported_with_reasons():
    """"Why didn't this alert?" must be answerable. Silent drops are the hardest failure
    to diagnose in a detection pipeline."""
    rule = Rule(
        type_code="zone.intrusion",
        alertable_classes=frozenset({"person"}),
        min_confidence=0.8,
        roi_polygon=CENTRE_SQUARE,
    )
    outcome = evaluate(
        rule,
        [
            obj(class_name="car"),                              # wrong class
            obj(confidence=0.5),                                # too low
            obj(bbox=(0.9, 0.9, 0.95, 0.95)),                   # outside ROI
            obj(),                                              # matches
        ],
        captured_at=NOON,
    )

    assert len(outcome.matched) == 1
    assert [reason for _, reason in outcome.rejected] == [
        RejectionReason.CLASS_NOT_ALERTABLE,
        RejectionReason.BELOW_CONFIDENCE,
        RejectionReason.OUTSIDE_ROI,
    ]


def test_best_returns_highest_confidence_match():
    rule = Rule(type_code="zone.intrusion", alertable_classes=frozenset({"person"}))
    outcome = evaluate(
        rule, [obj(confidence=0.6), obj(confidence=0.95), obj(confidence=0.7)], captured_at=NOON
    )
    assert outcome.best.confidence == 0.95
