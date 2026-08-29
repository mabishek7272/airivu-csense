"""Policy parsing and message rendering.

Policy JSON is tenant-editable, and the worker that reads it serves every tenant. So the
property that matters most here is not that a good policy parses - it is that a *bad* one
cannot take down alerting for everyone else. Every malformed-input test below is really
asking "does this raise", and the answer must always be no.

Pure functions only; no database, no network.
"""
from __future__ import annotations

import datetime as dt
import uuid

from csense_shared.notifications.escalation import (
    IncidentSummary,
    render_body,
    render_subject,
)
from csense_shared.notifications.policies import (
    MAX_DELAY_SECONDS,
    default_policy,
    parse_definition,
    quiet_hours_end,
    within_quiet_hours,
)
from csense_shared.notifications.providers import Channel

GROUP = str(uuid.uuid4())


def definition(**overrides) -> dict:
    base = {
        "severities": ["high", "critical"],
        "steps": [
            {
                "level": 0,
                "delay_seconds": 0,
                "channels": ["email", "whatsapp"],
                "recipient_group_ids": [GROUP],
            },
            {
                "level": 1,
                "delay_seconds": 600,
                "channels": ["email"],
                "recipient_group_ids": [GROUP],
            },
        ],
    }
    base.update(overrides)
    return base


# --- Well-formed input ----------------------------------------------------------------

def test_parses_steps_in_level_order():
    parsed = parse_definition(definition())

    assert [s.level for s in parsed.steps] == [0, 1]
    assert parsed.steps[0].channels == (Channel.EMAIL, Channel.WHATSAPP)
    assert parsed.steps[1].delay_seconds == 600
    assert parsed.applies_to("high")
    assert not parsed.applies_to("low")


def test_empty_severities_apply_to_everything():
    parsed = parse_definition(definition(severities=[]))

    assert parsed.applies_to("low")
    assert parsed.applies_to("critical")


# --- Malformed input must degrade, never raise ---------------------------------------

def test_non_dict_definition_returns_none():
    assert parse_definition(None) is None
    assert parse_definition("steps") is None
    assert parse_definition([1, 2, 3]) is None


def test_unknown_channel_is_dropped_not_fatal():
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": 0,
                    "channels": ["email", "telepathy"],
                    "recipient_group_ids": [GROUP],
                }
            ]
        )
    )

    assert parsed.steps[0].channels == (Channel.EMAIL,)


def test_step_with_no_usable_channel_is_dropped():
    """A step that can never reach anyone must not become an undeliverable queue row."""
    parsed = parse_definition(
        definition(
            steps=[
                {"level": 0, "channels": ["telepathy"], "recipient_group_ids": [GROUP]},
                {"level": 1, "channels": ["email"], "recipient_group_ids": [GROUP]},
            ]
        )
    )

    assert [s.level for s in parsed.steps] == [1]


def test_malformed_group_id_is_dropped():
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": 0,
                    "channels": ["email"],
                    "recipient_group_ids": ["not-a-uuid", GROUP],
                }
            ]
        )
    )

    assert parsed.steps[0].recipient_group_ids == (uuid.UUID(GROUP),)


def test_definition_with_nothing_usable_returns_none():
    """The caller then falls back to the default, rather than notifying nobody."""
    assert parse_definition(definition(steps=[])) is None
    assert parse_definition(definition(steps=[{"level": 0}])) is None
    assert parse_definition(definition(steps="not a list")) is None


def test_absurd_delay_is_capped_not_rejected():
    """Minutes typed into a seconds field. A late alert beats no alert."""
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": 0,
                    "delay_seconds": 999_999_999,
                    "channels": ["email"],
                    "recipient_group_ids": [GROUP],
                }
            ]
        )
    )

    assert parsed.steps[0].delay_seconds == MAX_DELAY_SECONDS


def test_negative_delay_becomes_immediate():
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": 0,
                    "delay_seconds": -600,
                    "channels": ["email"],
                    "recipient_group_ids": [GROUP],
                }
            ]
        )
    )

    assert parsed.steps[0].delay_seconds == 0


def test_duplicate_levels_are_collapsed():
    """The unique index would reject the second silently, leaving a step that never fires."""
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": 0,
                    "delay_seconds": 0,
                    "channels": ["email"],
                    "recipient_group_ids": [GROUP],
                },
                {
                    "level": 0,
                    "delay_seconds": 300,
                    "channels": ["whatsapp"],
                    "recipient_group_ids": [GROUP],
                },
            ]
        )
    )

    assert len(parsed.steps) == 1
    # The earlier of the two wins, so a duplicate cannot delay the first alert.
    assert parsed.steps[0].channels == (Channel.EMAIL,)


def test_step_count_is_bounded():
    parsed = parse_definition(
        definition(
            steps=[
                {
                    "level": i,
                    "delay_seconds": i * 60,
                    "channels": ["email"],
                    "recipient_group_ids": [GROUP],
                }
                for i in range(50)
            ]
        )
    )

    assert len(parsed.steps) <= 10


# --- Default policy -------------------------------------------------------------------

def test_default_policy_is_flat_and_immediate():
    groups = (uuid.uuid4(), uuid.uuid4())
    policy = default_policy(groups)

    assert len(policy.steps) == 1
    assert policy.steps[0].delay_seconds == 0
    assert policy.steps[0].recipient_group_ids == groups
    assert policy.applies_to("low")


# --- Quiet hours ----------------------------------------------------------------------

def test_quiet_hours_wrapping_midnight():
    """22:00-06:00 is the common case, and the one a naive comparison inverts."""
    quiet = {"start": "22:00", "end": "06:00"}

    assert within_quiet_hours(dt.datetime(2026, 8, 26, 23, 30, tzinfo=dt.UTC), quiet)
    assert within_quiet_hours(dt.datetime(2026, 8, 26, 2, 0, tzinfo=dt.UTC), quiet)
    assert not within_quiet_hours(dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC), quiet)
    assert not within_quiet_hours(dt.datetime(2026, 8, 26, 6, 0, tzinfo=dt.UTC), quiet)


def test_quiet_hours_same_day_window():
    quiet = {"start": "09:00", "end": "17:00"}

    assert within_quiet_hours(dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC), quiet)
    assert not within_quiet_hours(dt.datetime(2026, 8, 26, 20, 0, tzinfo=dt.UTC), quiet)


def test_malformed_quiet_hours_are_not_quiet():
    """Unreadable configuration must not silence alerts."""
    assert not within_quiet_hours(dt.datetime.now(dt.UTC), None)
    assert not within_quiet_hours(dt.datetime.now(dt.UTC), {"start": "nope", "end": "06:00"})
    assert not within_quiet_hours(dt.datetime.now(dt.UTC), {"start": "22:00"})
    # A zero-length window silences nothing.
    assert not within_quiet_hours(
        dt.datetime.now(dt.UTC), {"start": "10:00", "end": "10:00"}
    )


def test_quiet_hours_end_on_the_evening_side_of_a_wrap_is_tomorrow():
    quiet = {"start": "22:00", "end": "06:00"}
    end = quiet_hours_end(dt.datetime(2026, 8, 26, 23, 30, tzinfo=dt.UTC), quiet)
    assert end == dt.datetime(2026, 8, 27, 6, 0, tzinfo=dt.UTC)


def test_quiet_hours_end_on_the_morning_side_of_a_wrap_is_today():
    quiet = {"start": "22:00", "end": "06:00"}
    end = quiet_hours_end(dt.datetime(2026, 8, 26, 2, 0, tzinfo=dt.UTC), quiet)
    assert end == dt.datetime(2026, 8, 26, 6, 0, tzinfo=dt.UTC)


def test_quiet_hours_end_for_a_same_day_window():
    quiet = {"start": "09:00", "end": "17:00"}
    end = quiet_hours_end(dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC), quiet)
    assert end == dt.datetime(2026, 8, 26, 17, 0, tzinfo=dt.UTC)


def test_quiet_hours_end_applies_the_timezone_offset():
    # 23:30 UTC is 05:00 the next day at UTC+5:30 - past the 22:00-06:00 window's start
    # but still inside it locally, ending at 00:30 UTC (06:00 local).
    quiet = {"start": "22:00", "end": "06:00"}
    end = quiet_hours_end(
        dt.datetime(2026, 8, 26, 23, 30, tzinfo=dt.UTC), quiet, tz_offset_minutes=330
    )
    assert end == dt.datetime(2026, 8, 27, 0, 30, tzinfo=dt.UTC)


def test_quiet_hours_end_is_none_outside_the_window():
    quiet = {"start": "22:00", "end": "06:00"}
    assert quiet_hours_end(dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC), quiet) is None


def test_quiet_hours_end_is_none_for_unusable_configuration():
    assert quiet_hours_end(dt.datetime.now(dt.UTC), None) is None
    assert quiet_hours_end(dt.datetime.now(dt.UTC), {"start": "nope", "end": "06:00"}) is None


# --- Rendering ------------------------------------------------------------------------

def summary(**overrides) -> IncidentSummary:
    base = {
        "incident_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "incident_number": 42,
        "type_code": "zone.intrusion",
        "severity": "high",
        "title": "Intrusion",
        "camera_name": "Loading Bay 2",
        "site_name": "Chennai Depot",
        "site_address": {"line1": "12 Anna Salai", "city": "Chennai", "country": "India"},
        "first_detected_at": dt.datetime(2026, 8, 26, 21, 30, tzinfo=dt.UTC),
        "timezone_name": "Asia/Kolkata",
    }
    base.update(overrides)
    return IncidentSummary(**base)


def test_subject_names_the_place_and_the_event():
    subject = render_subject(summary(), level=0)

    assert "Intrusion" in subject
    assert "Chennai Depot" in subject
    assert "Urgent" in subject


def test_escalation_subject_says_it_is_an_escalation():
    """A second message that looks identical to the first gets ignored."""
    subject = render_subject(summary(), level=2)

    assert subject.startswith("[Escalation 2]")


def test_body_carries_what_a_responder_needs():
    body = render_body(summary(), level=0)

    assert "#42" in body
    assert "Loading Bay 2" in body
    assert "Chennai Depot" in body
    assert "12 Anna Salai, Chennai, India" in body
    assert "Acknowledge" in body


def test_time_is_rendered_in_the_site_timezone():
    """21:30 UTC is 03:00 next day in Kolkata. A guard should not convert at 3am."""
    body = render_body(summary(), level=0)

    assert "03:00" in body
    assert "27 Aug 2026" in body


def test_unknown_timezone_falls_back_to_utc_without_raising():
    body = render_body(summary(timezone_name="Mars/Olympus"), level=0)

    assert "21:30" in body


def test_malformed_address_costs_a_line_not_the_alert():
    body = render_body(summary(site_address="12 Anna Salai"), level=0)

    assert "Location:" not in body
    assert "#42" in body


def test_unmapped_type_code_still_produces_a_usable_alert():
    subject = render_subject(summary(type_code="custom.thing"), level=0)

    assert "custom.thing" in subject
