"""Turning an incident into a scheduled ladder of notifications.

**Every step is scheduled up front, not promoted one at a time.** `create_notification`
stamps each step with `scheduled_at = now + step.delay_seconds`, so writing the whole
ladder when the incident opens gives each rung its own firing time, and the worker simply
picks up whatever is due. The alternative - a promoter that wakes up and creates level N+1
when level N goes unanswered - has a failure mode this design does not: if the promoter
stops, escalation silently stops with it, and nobody finds out until an incident goes
unanswered. Here, a worker outage delays alerts; it cannot lose them.

Acknowledgement is what stops the ladder. `cancel_pending_for_incident` cancels every
notification that has not yet been sent, which is why the up-front schedule is safe: the
later rungs exist, but they never fire once a human has taken the incident.

**Rendering is deliberately plain text.** The subject and body are read on a phone at
night by someone who needs to know where to go and what they will find. Camera and site
names come from the tenant, so nothing here interpolates them into markup - the email
provider escapes them at render time.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.notifications.dispatcher import (
    create_notification,
    load_recipients,
)
from csense_shared.notifications.policies import ResolvedPolicy, resolve_policy

logger = logging.getLogger(__name__)

# Human wording for the event codes the pipeline emits. An unmapped code falls back to the
# code itself rather than being dropped - an alert reading "zone.intrusion" is ugly but
# actionable; no alert is not.
TYPE_LABELS = {
    "zone.intrusion": "Intrusion",
    "ppe.violation": "PPE violation",
    "fire.smoke": "Smoke or fire",
    "loitering": "Loitering",
    "crowd.density": "Crowd density",
    "vehicle.unauthorised": "Unauthorised vehicle",
    "fall.detected": "Fall detected",
}

SEVERITY_PREFIX = {
    "critical": "CRITICAL",
    "high": "Urgent",
    "medium": "",
    "low": "",
}


class IncidentSummary:
    """The fields an alert needs, gathered once so rendering does no further queries."""

    def __init__(
        self,
        *,
        incident_id: uuid.UUID,
        tenant_id: uuid.UUID,
        incident_number: int,
        type_code: str,
        severity: str,
        title: str,
        camera_name: str | None,
        site_name: str | None,
        site_address: str | None,
        first_detected_at: dt.datetime,
        timezone_name: str | None,
    ) -> None:
        self.incident_id = incident_id
        self.tenant_id = tenant_id
        self.incident_number = incident_number
        self.type_code = type_code
        self.severity = severity
        self.title = title
        self.camera_name = camera_name
        self.site_name = site_name
        self.site_address = site_address
        self.first_detected_at = first_detected_at
        self.timezone_name = timezone_name

    @property
    def label(self) -> str:
        return TYPE_LABELS.get(self.type_code, self.type_code)

    @property
    def address(self) -> str | None:
        """A one-line address from the site's stored JSON.

        The shape is tenant-supplied, so this reads the fields it knows in a sensible
        order and ignores the rest rather than assuming a schema. Anything unexpected
        yields no address line, which costs a detail - never the alert.
        """
        raw = self.site_address
        if not isinstance(raw, dict):
            return None
        parts = [
            str(raw[key]).strip()
            for key in ("line1", "line2", "city", "state", "postal_code", "country")
            if raw.get(key)
        ]
        joined = ", ".join(p for p in parts if p)
        return joined[:200] or None

    def local_time(self) -> str:
        """The detection time in the site's own timezone.

        A guard in Chennai should not have to convert from UTC at 3am, and an unknown or
        invalid zone must not stop the alert - it falls back to UTC, labelled as such.
        """
        moment = self.first_detected_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        if self.timezone_name:
            try:
                from zoneinfo import ZoneInfo

                moment = moment.astimezone(ZoneInfo(self.timezone_name))
            except Exception:  # noqa: BLE001 - any zone problem falls back to UTC
                logger.warning("unknown_timezone", extra={"tz": str(self.timezone_name)[:64]})
                moment = moment.astimezone(dt.UTC)
        else:
            moment = moment.astimezone(dt.UTC)
        return moment.strftime("%d %b %Y, %H:%M %Z").strip()


async def load_incident_summary(
    session: AsyncSession, *, incident_id: uuid.UUID
) -> IncidentSummary | None:
    row = (
        await session.execute(
            text(
                """
                SELECT i.id, i.tenant_id, i.incident_number, i.type_code, i.severity,
                       i.title, c.name, s.name, s.address_json, i.first_detected_at,
                       s.timezone
                FROM incidents i
                LEFT JOIN cameras c ON c.id = i.camera_id
                LEFT JOIN sites s ON s.id = i.site_id
                WHERE i.id = :id
                """
            ),
            {"id": incident_id},
        )
    ).first()
    if row is None:
        return None
    return IncidentSummary(
        incident_id=row[0],
        tenant_id=row[1],
        incident_number=row[2],
        type_code=row[3],
        severity=row[4],
        title=row[5],
        camera_name=row[6],
        site_name=row[7],
        site_address=row[8],
        first_detected_at=row[9],
        timezone_name=row[10],
    )


def render_subject(summary: IncidentSummary, *, level: int) -> str:
    prefix = SEVERITY_PREFIX.get(summary.severity, "")
    where = summary.site_name or summary.camera_name or "site"
    parts = [p for p in (prefix, f"{summary.label} at {where}") if p]
    subject = ": ".join(parts) if prefix else parts[0]
    if level > 0:
        # Says plainly why this arrived again, so a second message does not read as a
        # duplicate and get ignored.
        subject = f"[Escalation {level}] {subject}"
    return subject[:200]


def render_body(summary: IncidentSummary, *, level: int) -> str:
    """Plain text: what happened, where, when, and what to do."""
    lines = [
        f"{summary.label} detected.",
        "",
        f"Incident:  #{summary.incident_number}",
        f"Severity:  {summary.severity}",
    ]
    if summary.camera_name:
        lines.append(f"Camera:    {summary.camera_name}")
    if summary.site_name:
        lines.append(f"Site:      {summary.site_name}")
    if summary.address:
        lines.append(f"Location:  {summary.address}")
    lines.append(f"Time:      {summary.local_time()}")

    if level > 0:
        lines += [
            "",
            f"This is escalation level {level}. The incident has not been acknowledged.",
        ]

    lines += [
        "",
        "Acknowledge the incident in CSense to stop further escalation.",
    ]
    return "\n".join(lines)


async def schedule_incident_notifications(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    correlation_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
    policy: ResolvedPolicy | None = None,
) -> list[uuid.UUID]:
    """Writes the whole escalation ladder for an incident. Returns the notification ids.

    Safe to call more than once for the same incident: the unique index on
    (tenant, incident, level) makes each step idempotent, so a redelivered event or a
    retried transaction cannot double-notify.
    """
    moment = now or dt.datetime.now(dt.UTC)

    summary = await load_incident_summary(session, incident_id=incident_id)
    if summary is None:
        logger.warning("notify_unknown_incident", extra={"incident_id": str(incident_id)})
        return []

    resolved = policy or await resolve_policy(
        session,
        tenant_id=summary.tenant_id,
        severity=summary.severity,
        type_code=summary.type_code,
    )
    if resolved is None:
        return []

    created: list[uuid.UUID] = []
    for step in resolved.definition.steps:
        recipients = await load_recipients(
            session, tenant_id=summary.tenant_id, group_ids=step.recipient_group_ids
        )
        if not recipients:
            # An empty group is a configuration mistake worth seeing, not a crash.
            logger.warning(
                "escalation_step_has_no_recipients",
                extra={"incident_id": str(incident_id), "level": step.level},
            )
            continue

        notification_id = await create_notification(
            session,
            tenant_id=summary.tenant_id,
            incident_id=incident_id,
            policy_version_id=resolved.version_id,
            severity=summary.severity,
            step=step,
            subject=render_subject(summary, level=step.level),
            body=render_body(summary, level=step.level),
            recipients=recipients,
            now=moment,
            correlation_id=correlation_id,
        )
        if notification_id is not None:
            created.append(notification_id)

    if created:
        logger.info(
            "incident_notifications_scheduled",
            extra={
                "incident_id": str(incident_id),
                "tenant_id": str(summary.tenant_id),
                "levels": len(created),
                "policy": resolved.name,
            },
        )
    return created
