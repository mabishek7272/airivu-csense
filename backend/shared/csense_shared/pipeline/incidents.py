"""Turning rule matches into incident records.

Where rules.py is pure logic, this is the stateful half: it answers "is this already an
open incident?" and "what number does this tenant's next incident get?", both of which
need the database.

The correctness property that matters: **a rule firing on 25 consecutive frames must
produce one incident, not 25.** That is enforced by a partial unique index on
(tenant, camera, type, correlation_key) for non-closed incidents (migration 0009), so a
retry, a concurrent worker, or a redelivered event cannot slip past it. The application
path below takes the fast route - look up, then insert - and treats a unique violation as
"someone else won the race", which is the correct outcome rather than an error.

State transitions follow docs/05_BACKEND_SCHEMA.md §9.2 and every one appends an
`incident_events` row, which the database makes append-only.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.pipeline.rules import DetectedObject, Rule

# SCH §9.2. Terminal states have no outgoing transitions except reopening, which is
# deliberately not offered here - a resolved incident stays resolved, and a recurrence
# opens a new one so the history of each occurrence stays separate.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "open": {"acknowledged", "investigating", "escalated", "resolved", "dismissed"},
    "acknowledged": {"investigating", "escalated", "resolved", "dismissed"},
    "investigating": {"escalated", "resolved", "dismissed"},
    "escalated": {"investigating", "resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}

CLOSED_STATES = {"resolved", "dismissed"}


class InvalidTransitionError(ValueError):
    """Attempted an incident state change the state machine does not permit."""


@dataclass
class IncidentRecord:
    id: uuid.UUID
    incident_number: int
    status: str
    detection_count: int
    created: bool  # False when an existing open incident absorbed this detection


async def _next_incident_number(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Allocates the tenant's next incident number.

    Uses an upsert with a locked counter row rather than a global sequence: tenants see
    "Incident 42", and a shared sequence would leak how many incidents other tenants have
    generated. `ON CONFLICT DO UPDATE` takes the row lock, so concurrent callers serialise
    rather than collide.
    """
    result = await session.execute(
        text(
            """
            INSERT INTO tenant_counters (tenant_id, counter_name, current_value)
            VALUES (:tenant_id, 'incident_number', 1)
            ON CONFLICT (tenant_id, counter_name)
            DO UPDATE SET current_value = tenant_counters.current_value + 1,
                          updated_at = now()
            RETURNING current_value
            """
        ),
        {"tenant_id": tenant_id},
    )
    return int(result.scalar_one())


async def _find_live_incident(
    session: AsyncSession, *, tenant_id: uuid.UUID, camera_id: uuid.UUID, type_code: str, correlation_key: str
) -> tuple[uuid.UUID, int, int, dt.datetime] | None:
    result = await session.execute(
        text(
            """
            SELECT id, incident_number, detection_count, last_detected_at
            FROM incidents
            WHERE tenant_id = :tenant_id
              AND camera_id = :camera_id
              AND type_code = :type_code
              AND correlation_key = :correlation_key
              AND status NOT IN ('resolved', 'dismissed')
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "camera_id": camera_id,
            "type_code": type_code,
            "correlation_key": correlation_key,
        },
    )
    row = result.first()
    return (row[0], row[1], row[2], row[3]) if row else None


async def record_incident_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: str | None = None,
    payload: dict | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO incident_events
                (tenant_id, incident_id, event_type, actor_type, actor_id, payload,
                 previous_status, new_status, correlation_id)
            VALUES (:tenant_id, :incident_id, :event_type, :actor_type, :actor_id,
                    CAST(:payload AS jsonb), :previous_status, :new_status, :correlation_id)
            """
        ),
        {
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "payload": _json(payload or {}),
            "previous_status": previous_status,
            "new_status": new_status,
            "correlation_id": correlation_id,
        },
    )


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


async def upsert_incident_from_match(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    site_id: uuid.UUID,
    camera_id: uuid.UUID,
    rule: Rule,
    detected: DetectedObject,
    detection_id: str,
    captured_at: dt.datetime,
    zone_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> IncidentRecord:
    """Creates a new incident, or folds this detection into the live one.

    Returns `created=False` when an existing incident absorbed the detection, so the
    caller can decide whether to notify - a first occurrence usually warrants an alert, a
    continuation usually does not.
    """
    correlation_key = rule.correlation_key(str(camera_id), detected.class_name, zone_id)

    existing = await _find_live_incident(
        session, tenant_id=tenant_id, camera_id=camera_id, type_code=rule.type_code,
        correlation_key=correlation_key,
    )
    if existing is not None:
        incident_id, number, count, _ = existing
        await session.execute(
            text(
                """
                UPDATE incidents
                SET detection_count = detection_count + 1,
                    last_detected_at = GREATEST(last_detected_at, :captured_at),
                    updated_at = now(),
                    version = version + 1
                WHERE id = :incident_id
                """
            ),
            {"incident_id": incident_id, "captured_at": captured_at},
        )
        await _link_detection(session, tenant_id, incident_id, detection_id, captured_at, "continuation")
        return IncidentRecord(incident_id, number, "open", count + 1, created=False)

    number = await _next_incident_number(session, tenant_id)
    title = f"{rule.type_code.replace('.', ' ').title()}: {detected.class_name}"

    try:
        result = await session.execute(
            text(
                """
                INSERT INTO incidents
                    (tenant_id, site_id, camera_id, incident_number, type_code, severity,
                     status, title, summary, correlation_key, first_detected_at,
                     last_detected_at, detection_count, metadata)
                VALUES (:tenant_id, :site_id, :camera_id, :incident_number, :type_code,
                        CAST(:severity AS incident_severity), 'open', :title, :summary,
                        :correlation_key, :captured_at, :captured_at, 1, CAST(:metadata AS jsonb))
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "incident_number": number,
                "type_code": rule.type_code,
                "severity": rule.severity,
                "title": title,
                "summary": (
                    f"{detected.class_name} detected with confidence "
                    f"{detected.confidence:.2f}"
                ),
                "correlation_key": correlation_key,
                "captured_at": captured_at,
                "metadata": _json(
                    {
                        "trigger_confidence": round(detected.confidence, 4),
                        "trigger_bbox": [round(v, 5) for v in detected.bbox],
                        "zone_id": zone_id,
                        "rule": {
                            "min_confidence": rule.min_confidence,
                            "min_roi_overlap": rule.min_roi_overlap,
                            "min_consecutive_frames": rule.min_consecutive_frames,
                        },
                    }
                ),
            },
        )
        incident_id = result.scalar_one()
    except IntegrityError:
        # Another worker created the same incident between our lookup and insert. The
        # partial unique index did its job; adopt the winner rather than failing.
        await session.rollback()
        existing = await _find_live_incident(
            session, tenant_id=tenant_id, camera_id=camera_id, type_code=rule.type_code,
            correlation_key=correlation_key,
        )
        if existing is None:
            raise
        incident_id, number, count, _ = existing
        await _link_detection(session, tenant_id, incident_id, detection_id, captured_at, "continuation")
        return IncidentRecord(incident_id, number, "open", count, created=False)

    await _link_detection(session, tenant_id, incident_id, detection_id, captured_at, "rule_match")
    await record_incident_event(
        session,
        tenant_id=tenant_id,
        incident_id=incident_id,
        event_type="incident.created",
        actor_type="pipeline",
        actor_id=rule.type_code,
        payload={"correlation_key": correlation_key, "class_name": detected.class_name},
        new_status="open",
        correlation_id=correlation_id,
    )
    return IncidentRecord(incident_id, number, "open", 1, created=True)


async def _link_detection(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    detection_id: str,
    captured_at: dt.datetime,
    reason: str,
) -> None:
    # ON CONFLICT DO NOTHING makes redelivery of the same detection harmless, which
    # at-least-once event delivery guarantees will happen (TRD §11.3).
    await session.execute(
        text(
            """
            INSERT INTO incident_detection_links
                (tenant_id, incident_id, detection_id, capture_time, link_reason)
            VALUES (:tenant_id, :incident_id, :detection_id, :capture_time, :reason)
            ON CONFLICT (incident_id, detection_id) DO NOTHING
            """
        ),
        {
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "detection_id": detection_id,
            "capture_time": captured_at,
            "reason": reason,
        },
    )


async def transition_incident(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    new_status: str,
    actor_type: str,
    actor_id: str | None,
    reason: str | None = None,
    resolution_code: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> str:
    """Moves an incident through the state machine, appending history.

    Raises InvalidTransitionError rather than silently no-oping: an operator who thinks
    they resolved something that is still open is worse than an error message.
    """
    result = await session.execute(
        text("SELECT status FROM incidents WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": incident_id, "tenant_id": tenant_id},
    )
    row = result.first()
    if row is None:
        raise InvalidTransitionError("Incident not found in this tenant.")

    current = row[0]
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Cannot move an incident from '{current}' to '{new_status}'. "
            f"Allowed from here: {', '.join(sorted(allowed)) or 'none (terminal state)'}."
        )

    timestamps = ""
    if new_status == "acknowledged":
        timestamps = ", acknowledged_at = now(), acknowledged_by = :actor_uuid"
    elif new_status in CLOSED_STATES:
        timestamps = ", resolved_at = now(), resolved_by = :actor_uuid, resolution_code = :resolution_code"

    params: dict = {
        "id": incident_id,
        "tenant_id": tenant_id,
        "status": new_status,
    }
    if timestamps:
        params["actor_uuid"] = _as_uuid(actor_id)
    if new_status in CLOSED_STATES:
        params["resolution_code"] = resolution_code

    await session.execute(
        text(
            f"""
            UPDATE incidents
            SET status = CAST(:status AS incident_status){timestamps},
                updated_at = now(), version = version + 1
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        params,
    )

    await record_incident_event(
        session,
        tenant_id=tenant_id,
        incident_id=incident_id,
        event_type=f"incident.{new_status}",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"reason": reason} if reason else {},
        previous_status=current,
        new_status=new_status,
        correlation_id=correlation_id,
    )
    return current


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        # Non-user actors (pipeline, worker) have string ids; the column only accepts a
        # user reference, so leave it null rather than inventing one.
        return None
