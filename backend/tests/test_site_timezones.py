"""Timezone validation for sites.

The zone is the one field on a site that changes behaviour: rule schedules are written in
local time and converted using it before evaluation, so a wrong or unusable value produces
no error at all â€” just rules firing at the wrong hour.

The case worth pinning down is the one that was originally got wrong. Validating against
`zoneinfo.available_timezones()` looks correct and is subtly too strict: it omits
deprecated aliases, and browsers still report several of them. `Intl.DateTimeFormat()`
returns `Asia/Calcutta` on many systems, which `ZoneInfo` resolves perfectly well â€” so
rejecting it refused a zone the pipeline could have used, and the site could not be
created at all from an Indian browser.

No database, no network.
"""
from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest

from csense_shared.timezones import OFFERED_TIMEZONES, is_usable_timezone


@pytest.mark.parametrize(
    "zone",
    ["UTC", "Asia/Kolkata", "Australia/Sydney", "Europe/London", "America/New_York"],
)
def test_canonical_zones_are_accepted(zone):
    assert is_usable_timezone(zone)


@pytest.mark.parametrize(
    "alias,canonical",
    [
        # What Intl.DateTimeFormat reports on many Indian systems.
        ("Asia/Calcutta", "Asia/Kolkata"),
        ("Asia/Saigon", "Asia/Ho_Chi_Minh"),
        ("America/Buenos_Aires", "America/Argentina/Buenos_Aires"),
        ("Europe/Kiev", "Europe/Kyiv"),
    ],
)
def test_deprecated_aliases_are_accepted(alias, canonical):
    """A browser reporting an alias must still be able to create a site.

    This is the regression: these all resolve, and all of them were refused when the check
    was membership of `available_timezones()`.
    """
    assert is_usable_timezone(alias), f"{alias} should resolve"

    # And it really is the same zone, so accepting it costs nothing in correctness.
    moment = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)
    assert moment.astimezone(zoneinfo.ZoneInfo(alias)).utcoffset() == moment.astimezone(
        zoneinfo.ZoneInfo(canonical)
    ).utcoffset()


@pytest.mark.parametrize(
    "zone",
    ["", "Mars/Olympus", "Not A Zone", "asia/kolkata", "GMT+5:30", "../../etc/passwd"],
)
def test_unusable_values_are_refused(zone):
    """Including a traversal-shaped string: zone names reach a file lookup."""
    assert not is_usable_timezone(zone)


def test_the_offered_list_is_a_subset_of_what_is_accepted():
    """The picker must never offer something the validator would reject.

    A dropdown that produces a 422 is worse than a free-text field, because the user has
    no reason to suspect their choice was the problem. The reverse - accepting more than
    is offered - is fine, and is what makes aliases work.
    """
    assert OFFERED_TIMEZONES, "the picker would be empty"
    unusable = [z for z in OFFERED_TIMEZONES if not is_usable_timezone(z)]
    assert unusable == [], f"offered but not accepted: {unusable[:5]}"


def test_canonical_zones_are_always_offered():
    assert "Asia/Kolkata" in OFFERED_TIMEZONES
    assert "UTC" in OFFERED_TIMEZONES


def test_alias_acceptance_does_not_depend_on_the_offered_list():
    """The point of the fix, stated as an invariant rather than a set membership.

    Whether `available_timezones()` contains an alias varies by environment: it does on a
    Windows host using the `tzdata` package, and did not in the Debian-based container
    where this first failed. That inconsistency is precisely why membership was the wrong
    check - the same request succeeded locally and 422'd in Docker.

    Resolution does not vary, so acceptance must not either.
    """
    for alias in ("Asia/Calcutta", "Asia/Saigon", "Europe/Kiev"):
        assert is_usable_timezone(alias), (
            f"{alias} resolves via ZoneInfo and must be accepted whether or not this "
            "environment happens to list it"
        )
