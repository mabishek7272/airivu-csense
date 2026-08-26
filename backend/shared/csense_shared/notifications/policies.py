"""Resolving which notification policy applies to an incident, and parsing it.

A policy version's `definition_json` is written by tenants through the CRM, and it is the
input that decides who gets woken at 3am. Two consequences shape this module:

**Parsing must never raise.** A malformed policy that threw mid-dispatch would take down
the worker for every tenant, not just the one with the bad policy. Every field is parsed
defensively; anything unusable is dropped with a log line and the rest of the policy still
runs.

**There is always a policy.** A tenant who has configured nothing must still be told when
someone climbs their fence. `default_policy` is that floor: notify every recipient group
immediately, on whichever channels each recipient chose. It is deliberately flat - no
escalation ladder is better than guessing at one - and it applies only when the tenant has
published nothing that matches.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.notifications.dispatcher import EscalationStep, PolicyDefinition
from csense_shared.notifications.providers import Channel

logger = logging.getLogger(__name__)

# A delay beyond this is almost certainly a units mistake - minutes typed into a seconds
# field. Capped rather than rejected: a late alert beats no alert.
MAX_DELAY_SECONDS = 24 * 60 * 60
MAX_STEPS = 10


class ResolvedPolicy:
    """A policy definition plus the version id to record on each notification.

    The version id matters for audit: "why was I called at 3am" must be answerable months
    later, after the policy has been edited. That is only possible if the notification
    points at the immutable version that was in force at the time.
    """

    def __init__(self, definition: PolicyDefinition, version_id: uuid.UUID | None, name: str):
        self.definition = definition
        self.version_id = version_id
        self.name = name

    @property
    def is_default(self) -> bool:
        return self.version_id is None


def _parse_channels(raw: object) -> tuple[Channel, ...]:
    """Channels a step may use, ignoring anything unrecognised."""
    if not isinstance(raw, list):
        return ()
    out: list[Channel] = []
    for item in raw:
        try:
            channel = Channel(str(item))
        except ValueError:
            logger.warning("policy_unknown_channel", extra={"channel": str(item)[:32]})
            continue
        if channel not in out:
            out.append(channel)
    return tuple(out)


def _parse_group_ids(raw: object) -> tuple[uuid.UUID, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            group = uuid.UUID(str(item))
        except (ValueError, AttributeError, TypeError):
            logger.warning("policy_bad_group_id", extra={"value": str(item)[:64]})
            continue
        if group not in out:
            out.append(group)
    return tuple(out)


def parse_definition(raw: object) -> PolicyDefinition | None:
    """Turns stored JSON into a PolicyDefinition, or None if nothing usable survives."""
    if not isinstance(raw, dict):
        return None

    severities = frozenset(
        str(s) for s in raw.get("severities", []) if isinstance(s, (str, int))
    )

    steps: list[EscalationStep] = []
    for entry in (raw.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(entry, dict):
            continue
        channels = _parse_channels(entry.get("channels"))
        groups = _parse_group_ids(entry.get("recipient_group_ids"))
        # A step with no channels or no groups can never reach anyone. Keeping it would
        # create notification rows that are permanently undeliverable.
        if not channels or not groups:
            logger.warning("policy_step_unusable", extra={"level": entry.get("level")})
            continue
        try:
            level = int(entry.get("level", len(steps)))
            delay = int(entry.get("delay_seconds", 0))
        except (TypeError, ValueError):
            continue
        steps.append(
            EscalationStep(
                level=max(0, level),
                delay_seconds=min(max(0, delay), MAX_DELAY_SECONDS),
                channels=channels,
                recipient_group_ids=groups,
            )
        )

    if not steps:
        return None

    # Deduplicate by level. The unique index on (tenant, incident, level) would reject a
    # duplicate anyway, but silently - leaving a step that looks scheduled and never fires.
    by_level: dict[int, EscalationStep] = {}
    for step in sorted(steps, key=lambda s: s.delay_seconds):
        by_level.setdefault(step.level, step)

    return PolicyDefinition(
        steps=tuple(sorted(by_level.values(), key=lambda s: s.level)),
        severities=severities,
    )


def default_policy(group_ids: tuple[uuid.UUID, ...]) -> PolicyDefinition:
    """The floor: one immediate step to every group, on every channel they accept.

    `load_recipients` filters channels against each recipient's own preference, so
    offering three here does not mean sending three times to one person - it means each
    person is reached the way they asked to be.
    """
    return PolicyDefinition(
        steps=(
            EscalationStep(
                level=0,
                delay_seconds=0,
                channels=(Channel.EMAIL, Channel.WHATSAPP, Channel.IN_APP),
                recipient_group_ids=group_ids,
            ),
        ),
        severities=frozenset(),
    )


async def all_recipient_group_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> tuple[uuid.UUID, ...]:
    rows = (
        await session.execute(
            text("SELECT id FROM recipient_groups WHERE tenant_id = :t ORDER BY created_at"),
            {"t": tenant_id},
        )
    ).scalars().all()
    return tuple(rows)


def _filter_matches(event_filter: object, *, type_code: str, severity: str) -> bool:
    """Whether a policy's event filter selects this incident.

    An empty or unreadable filter matches everything. That is the safe direction: a filter
    nobody can parse should over-notify rather than go silent.
    """
    if not isinstance(event_filter, dict) or not event_filter:
        return True

    types = event_filter.get("type_codes")
    if isinstance(types, list) and types and type_code not in {str(t) for t in types}:
        return False

    severities = event_filter.get("severities")
    if isinstance(severities, list) and severities and severity not in {str(s) for s in severities}:
        return False

    return True


async def resolve_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    severity: str,
    type_code: str,
) -> ResolvedPolicy | None:
    """The active policy for this incident, or the default, or None if nobody can be told.

    Returns None only when the tenant has no recipient groups at all. There is then
    genuinely nobody to notify, and creating notification rows addressed to no one would
    fill the queue with permanent failures.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT p.name, p.event_filter, v.id, v.definition_json
                FROM notification_policies p
                JOIN notification_policy_versions v ON v.id = p.active_version_id
                WHERE p.tenant_id = :t AND p.status = 'active'
                ORDER BY p.created_at
                """
            ),
            {"t": tenant_id},
        )
    ).all()

    for name, event_filter, version_id, definition_json in rows:
        if not _filter_matches(event_filter, type_code=type_code, severity=severity):
            continue
        definition = parse_definition(definition_json)
        if definition is None:
            logger.warning(
                "policy_definition_unusable",
                extra={"tenant_id": str(tenant_id), "policy": name},
            )
            continue
        if not definition.applies_to(severity):
            continue
        return ResolvedPolicy(definition, version_id, name)

    groups = await all_recipient_group_ids(session, tenant_id=tenant_id)
    if not groups:
        logger.warning(
            "no_recipients_configured",
            extra={"tenant_id": str(tenant_id), "type_code": type_code},
        )
        return None
    return ResolvedPolicy(default_policy(groups), None, "Default (no policy configured)")


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
