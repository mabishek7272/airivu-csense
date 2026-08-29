"""Which timezone names this deployment can actually use.

Lives in the shared library rather than in an API module because two different parts of
the system depend on the same answer: the API decides whether a site's zone may be saved,
and the pipeline resolves that zone every time it evaluates a time-based rule. If those
two disagreed, a site could hold a value that the evaluator later fails on — silently,
because a failed conversion falls back to UTC and the rule simply runs at the wrong hour.

**Validity is `ZoneInfo(value)` succeeding, not membership of `available_timezones()`.**
That distinction caused a real bug. `available_timezones()` omits deprecated aliases, and
browsers still report several: `Intl.DateTimeFormat().resolvedOptions().timeZone` returns
`Asia/Calcutta` rather than `Asia/Kolkata` on many systems. Validating against the set
refused a zone `ZoneInfo` resolves perfectly well, and made it impossible to create a site
at all from an Indian browser. The honest test is whether the thing the pipeline will do
later succeeds now.
"""
from __future__ import annotations

import zoneinfo

# What a picker should offer: canonical names only. Narrower than what is *accepted* -
# `Asia/Calcutta` is valid but not worth offering when `Asia/Kolkata` names the same zone.
# The subset direction is the one that matters: a picker must never produce a value the
# validator rejects.
OFFERED_TIMEZONES: list[str] = sorted(zoneinfo.available_timezones())


def is_usable_timezone(value: str) -> bool:
    """Whether the pipeline will be able to resolve this zone.

    Zone names reach a filesystem lookup, so anything unresolvable - including
    traversal-shaped strings - is refused here rather than at the point of use.
    """
    if not value or not isinstance(value, str):
        return False
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return False
    return True
