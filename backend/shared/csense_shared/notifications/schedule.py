"""Quiet hours: whether a moment falls inside a recipient's own do-not-disturb window.

Lives in its own module rather than `policies.py` or `dispatcher.py` because both need
it and importing between those two would be circular - `policies.py` already imports
`EscalationStep`/`PolicyDefinition` from `dispatcher.py`, and `dispatcher.py` needs this
to decide, per recipient, whether tonight's non-urgent notification waits until morning.

These two functions are pure and never raise - the input is JSONB a person filled in
through a form, and a malformed value must read as "not quiet" rather than take down
delivery for everyone else.
"""
from __future__ import annotations

import datetime as dt


def within_quiet_hours(
    moment: dt.datetime, quiet: object, *, tz_offset_minutes: int = 0
) -> bool:
    """Whether a moment falls inside a configured quiet window.

    Handles windows that wrap midnight (22:00-06:00), which is the common case and the one
    a naive start <= t < end comparison gets exactly backwards.
    """
    if not isinstance(quiet, dict):
        return False
    start = quiet.get("start")
    end = quiet.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return False
    try:
        start_h, start_m = (int(p) for p in start.split(":", 1))
        end_h, end_m = (int(p) for p in end.split(":", 1))
    except (ValueError, TypeError):
        return False

    local = moment + dt.timedelta(minutes=tz_offset_minutes)
    minutes = local.hour * 60 + local.minute
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m

    if start_total == end_total:
        return False
    if start_total < end_total:
        return start_total <= minutes < end_total
    return minutes >= start_total or minutes < end_total


def quiet_hours_end(
    moment: dt.datetime, quiet: object, *, tz_offset_minutes: int = 0
) -> dt.datetime | None:
    """The next moment `moment` is no longer inside its quiet window - or None if
    `moment` is not inside one right now.

    A delivery held for quiet hours needs to know when to actually go out, not just that
    it should wait. This finds *this* window's boundary; it does not search forward past
    it, because `within_quiet_hours` already established `moment` is inside it.
    """
    if not within_quiet_hours(moment, quiet, tz_offset_minutes=tz_offset_minutes):
        return None

    start_h, start_m = (int(p) for p in quiet["start"].split(":", 1))
    end_h, end_m = (int(p) for p in quiet["end"].split(":", 1))
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m

    local = moment + dt.timedelta(minutes=tz_offset_minutes)
    minutes = local.hour * 60 + local.minute

    end_date = local.date()
    if start_total > end_total and minutes >= start_total:
        # The evening side of a window that wraps midnight (e.g. 23:30 inside
        # 22:00-06:00) - the end time falls on the next calendar day, not this one.
        end_date += dt.timedelta(days=1)

    end_local = dt.datetime(
        end_date.year, end_date.month, end_date.day, end_h, end_m, tzinfo=local.tzinfo
    )
    return end_local - dt.timedelta(minutes=tz_offset_minutes)
