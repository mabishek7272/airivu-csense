"""Rule evaluation: raw detections in, incident intents out.

This is the stage between inference and the incident record (TRD §16-17). A detection is
an immutable observation; an incident is a business case. Not every detection deserves
one, and the gap between them is where false-positive fatigue is won or lost - the
implementation plan lists "false positives delay customer acceptance" as a top risk.

Four filters, applied in order, cheapest first:

  1. **Class filter** - is this class alertable at all? The legacy kitchen-safety model
     emits `maskon` and `glove` (compliant) alongside `no_glove` (a violation). Alerting on
     compliance would make the feature unusable.
  2. **Confidence threshold** - per rule, since a fire model at 0.3 and a person detector
     at 0.3 do not carry the same weight.
  3. **Region of interest** - was the object inside the zone that matters? A person in the
     corridor is not a person in the restricted area.
  4. **Minimum duration** - did it persist? A single frame is usually noise; requiring N
     consecutive detections removes most flicker.

Cooldown and correlation are handled by the caller, which has the database, because
"has this already raised an incident" is a question only shared state can answer.

Pure functions: no database, no clock, no I/O. Time is passed in. That keeps the logic
directly testable, which matters because these thresholds are what operators will tune.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum


class RejectionReason(StrEnum):
    """Why a detection did not become an incident.

    Recorded rather than discarded: when an operator asks "why didn't this alert?", the
    answer must be recoverable. Silent drops are the hardest class of bug to diagnose in a
    detection pipeline.
    """

    CLASS_NOT_ALERTABLE = "class_not_alertable"
    BELOW_CONFIDENCE = "below_confidence"
    OUTSIDE_ROI = "outside_roi"
    INSUFFICIENT_DURATION = "insufficient_duration"
    WITHIN_COOLDOWN = "within_cooldown"
    OUTSIDE_SCHEDULE = "outside_schedule"


@dataclass(frozen=True)
class DetectedObject:
    """One object from the runtime, in normalised coordinates."""

    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]
    track_id: str | None = None


@dataclass(frozen=True)
class Rule:
    """A rule definition, as it will be stored in a published pipeline version."""

    type_code: str
    alertable_classes: frozenset[str]
    min_confidence: float = 0.5
    severity: str = "medium"
    # Zone polygon in normalised coordinates; None means the whole frame.
    roi_polygon: tuple[tuple[float, float], ...] | None = None
    # How much of the object's box must fall inside the ROI. Centre-point containment is
    # too eager for large objects and too strict for partially-occluded ones.
    min_roi_overlap: float = 0.3
    # Consecutive qualifying frames required before this becomes an incident.
    min_consecutive_frames: int = 1
    # Suppress a repeat incident for the same correlation key within this window.
    cooldown_seconds: int = 300
    # Hour-of-day window in the site's timezone, e.g. (22, 6) for overnight. None = always.
    active_hours: tuple[int, int] | None = None

    def correlation_key(self, camera_id: str, class_name: str, zone_id: str | None) -> str:
        """Identity of "the same ongoing situation".

        Deliberately excludes track_id: a person who leaves and re-enters the frame gets a
        new track but is the same situation, and re-alerting on every re-acquisition is the
        classic cause of alert storms.
        """
        return f"{self.type_code}:{camera_id}:{class_name}:{zone_id or 'frame'}"


@dataclass
class RuleOutcome:
    matched: list[DetectedObject] = field(default_factory=list)
    rejected: list[tuple[DetectedObject, RejectionReason]] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return bool(self.matched)

    @property
    def best(self) -> DetectedObject | None:
        return max(self.matched, key=lambda o: o.confidence) if self.matched else None


def _polygon_area(polygon: tuple[tuple[float, float], ...]) -> float:
    """Shoelace formula. Absolute value, so vertex winding order does not matter."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _clip_polygon_to_box(
    polygon: tuple[tuple[float, float], ...], box: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a polygon against an axis-aligned rectangle.

    Used to compute genuine overlap area rather than approximating with a centre point.
    An intruder standing at the boundary of a restricted zone is exactly the case where
    the approximation gives the wrong answer.
    """
    x1, y1, x2, y2 = box
    output = list(polygon)

    # Each edge as (inside-test, intersection-parameter axis).
    for axis, bound, keep_greater in ((0, x1, True), (0, x2, False), (1, y1, True), (1, y2, False)):
        if not output:
            return []
        input_list, output = output, []
        for i in range(len(input_list)):
            current = input_list[i]
            previous = input_list[i - 1]

            def inside(point, axis=axis, bound=bound, keep_greater=keep_greater):
                return point[axis] >= bound if keep_greater else point[axis] <= bound

            if inside(current):
                if not inside(previous):
                    output.append(_intersect(previous, current, axis, bound))
                output.append(current)
            elif inside(previous):
                output.append(_intersect(previous, current, axis, bound))
    return output


def _intersect(p1, p2, axis: int, bound: float) -> tuple[float, float]:
    other = 1 - axis
    span = p2[axis] - p1[axis]
    if span == 0:
        return (bound, p1[other]) if axis == 0 else (p1[other], bound)
    t = (bound - p1[axis]) / span
    value = p1[other] + t * (p2[other] - p1[other])
    return (bound, value) if axis == 0 else (value, bound)


def roi_overlap_fraction(
    bbox: tuple[float, float, float, float], polygon: tuple[tuple[float, float], ...] | None
) -> float:
    """Fraction of the detection box that lies inside the ROI polygon (0..1).

    Returns 1.0 when there is no ROI - no zone means the whole frame is in scope.
    """
    if polygon is None:
        return 1.0

    x1, y1, x2, y2 = bbox
    box_area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if box_area <= 0:
        return 0.0

    clipped = _clip_polygon_to_box(polygon, (x1, y1, x2, y2))
    if len(clipped) < 3:
        return 0.0
    return min(1.0, _polygon_area(tuple(clipped)) / box_area)


def within_active_hours(rule: Rule, moment: dt.datetime) -> bool:
    """Hour-window check, handling windows that wrap past midnight.

    An overnight intrusion rule is (22, 6), which is *not* the range 22..6 read literally -
    getting this wrong disables the rule for its entire intended window.
    """
    if rule.active_hours is None:
        return True
    start, end = rule.active_hours
    hour = moment.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def evaluate(
    rule: Rule,
    objects: list[DetectedObject],
    *,
    captured_at: dt.datetime,
    consecutive_frames: int = 1,
) -> RuleOutcome:
    """Applies a rule to one frame's detections.

    `consecutive_frames` is how many prior frames in a row already qualified, tracked by
    the caller across frames. Passed in rather than held here so this stays pure.
    """
    outcome = RuleOutcome()

    if not within_active_hours(rule, captured_at):
        for obj in objects:
            outcome.rejected.append((obj, RejectionReason.OUTSIDE_SCHEDULE))
        return outcome

    for obj in objects:
        if obj.class_name not in rule.alertable_classes:
            outcome.rejected.append((obj, RejectionReason.CLASS_NOT_ALERTABLE))
            continue
        if obj.confidence < rule.min_confidence:
            outcome.rejected.append((obj, RejectionReason.BELOW_CONFIDENCE))
            continue
        if roi_overlap_fraction(obj.bbox, rule.roi_polygon) < rule.min_roi_overlap:
            outcome.rejected.append((obj, RejectionReason.OUTSIDE_ROI))
            continue
        if consecutive_frames < rule.min_consecutive_frames:
            outcome.rejected.append((obj, RejectionReason.INSUFFICIENT_DURATION))
            continue
        outcome.matched.append(obj)

    return outcome


def is_within_cooldown(
    rule: Rule, last_incident_at: dt.datetime | None, now: dt.datetime
) -> bool:
    """Whether a repeat incident should be suppressed.

    Cooldown is measured from the previous incident's *last* detection, so a situation that
    persists keeps one incident open rather than reopening a new one every window.
    """
    if last_incident_at is None or rule.cooldown_seconds <= 0:
        return False
    return (now - last_incident_at).total_seconds() < rule.cooldown_seconds
