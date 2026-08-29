"""Detection ingestion: the path from "a model saw something" to "a person was told".

Every piece of this existed already - rule evaluation, detection persistence, incident
correlation, evidence capture, notification scheduling - and nothing joined them up
outside a demo script. That meant the system could not process a single real event. This
module is the join.

The whole chain runs in **one transaction**, deliberately. A detection recorded without
its incident, or an incident with no notifications scheduled, is worse than nothing: it
looks handled and nobody was told. Either all of it lands or none of it does, and the
caller retries.

**Idempotency comes from the edge, not from us.** An edge device assigns each event a
`source_event_id` and will resend after a network drop, because the alternative is losing
events. `record_detection` upserts on `(tenant_id, source_event_id)`, and incident
correlation folds a repeat into the live incident rather than opening a second one, so a
replay is a no-op rather than a duplicate 3am call.

**Evidence is captured only when an incident opens.** A camera watching a person walk
across a yard produces a detection per frame; storing an annotated snapshot for each would
multiply storage by the frame rate for no operational gain. The frame that opened the
incident is the one that gets kept.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.notifications.escalation import schedule_incident_notifications
from csense_shared.pipeline.detections import (
    DetectionDocument,
    attach_evidence_ref,
    record_detection,
)
from csense_shared.pipeline.evidence import Annotation, capture_evidence
from csense_shared.pipeline.incidents import upsert_incident_from_match
from csense_shared.pipeline.rules import DetectedObject, Rule, evaluate

logger = logging.getLogger(__name__)

# An upper bound on objects accepted from one frame. A malfunctioning or hostile edge
# reporting fifty thousand boxes must not be able to stall ingestion for a whole tenant.
MAX_OBJECTS_PER_FRAME = 300


@dataclass(frozen=True)
class IngestResult:
    """What ingestion did, in terms the caller can act on."""

    detection_id: str
    duplicate: bool
    rules_evaluated: int
    incident_id: uuid.UUID | None = None
    incident_number: int | None = None
    incident_created: bool = False
    notifications_scheduled: int = 0
    evidence_captured: bool = False
    rejected_reasons: list[str] = field(default_factory=list)


def _polygon_from_zone(geometry: object) -> tuple[tuple[float, float], ...] | None:
    """Normalised polygon from a zone's stored geometry.

    Zone geometry is tenant-supplied through the CRM, so anything unusable yields None -
    a whole-frame rule - rather than raising. A rule that watches too much still alerts;
    one that raises stops ingestion for every camera at the site.
    """
    if not isinstance(geometry, dict):
        return None
    points = geometry.get("polygon") or geometry.get("points")
    if not isinstance(points, list) or len(points) < 3:
        return None

    polygon: list[tuple[float, float]] = []
    for point in points:
        try:
            if isinstance(point, dict):
                x, y = float(point["x"]), float(point["y"])
            else:
                x, y = float(point[0]), float(point[1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        polygon.append((x, y))
    return tuple(polygon)


def _to_site_time(moment: dt.datetime, timezone_name: str | None) -> dt.datetime:
    """The same instant expressed in the site's timezone.

    Rule schedules are written by people thinking in local time - "after 22:00" means
    22:00 at the gate, not in UTC. An unknown or malformed zone falls back to UTC with a
    log line rather than raising: a rule evaluated in the wrong timezone is a bug worth
    fixing, but refusing to evaluate it at all means no alert.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    if not timezone_name:
        return moment
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001 - any zone problem degrades to UTC
        logger.warning("unknown_site_timezone", extra={"tz": str(timezone_name)[:64]})
        return moment


async def load_rules(
    session: AsyncSession, *, tenant_id: uuid.UUID, camera_id: uuid.UUID
) -> list[tuple[Rule, uuid.UUID | None]]:
    """Active rules for a camera, each paired with the zone it watches.

    Includes site-wide rules (camera_id IS NULL) alongside camera-specific ones: "nobody
    in the yard after 22:00" is configured once for the site, not forty times.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT r.type_code, r.alertable_classes, r.min_confidence, r.severity,
                       r.min_roi_overlap, r.min_consecutive_frames, r.cooldown_seconds,
                       r.active_from_hour, r.active_to_hour, r.zone_id, z.geometry_json
                FROM detection_rules r
                JOIN cameras c ON c.id = :camera_id
                LEFT JOIN zones z ON z.id = r.zone_id AND z.status = 'active'
                WHERE r.tenant_id = :tenant_id
                  AND r.status = 'active'
                  AND r.site_id = c.site_id
                  AND (r.camera_id IS NULL OR r.camera_id = :camera_id)
                ORDER BY r.created_at
                """
            ),
            {"tenant_id": tenant_id, "camera_id": camera_id},
        )
    ).all()

    rules: list[tuple[Rule, uuid.UUID | None]] = []
    for row in rows:
        classes = row[1]
        if not isinstance(classes, list) or not classes:
            # The database constrains this, but a rule that can never match is worth a
            # log line rather than a silent skip.
            logger.warning("rule_has_no_classes", extra={"type_code": row[0]})
            continue

        active_hours = None
        if row[7] is not None and row[8] is not None:
            active_hours = (int(row[7]), int(row[8]))

        rules.append(
            (
                Rule(
                    type_code=row[0],
                    alertable_classes=frozenset(str(c) for c in classes),
                    min_confidence=float(row[2]),
                    severity=row[3],
                    roi_polygon=_polygon_from_zone(row[10]),
                    min_roi_overlap=float(row[4]),
                    min_consecutive_frames=int(row[5]),
                    cooldown_seconds=int(row[6]),
                    active_hours=active_hours,
                ),
                row[9],
            )
        )
    return rules


async def ingest_detection(
    session: AsyncSession,
    object_store,
    *,
    tenant_id: uuid.UUID,
    site_id: uuid.UUID,
    camera_id: uuid.UUID,
    source_event_id: str,
    captured_at: dt.datetime,
    objects: list[DetectedObject],
    frame: object = None,
    model_version_id: uuid.UUID | None = None,
    edge_device_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    site_timezone: str | None = None,
    now: dt.datetime | None = None,
) -> IngestResult:
    """Runs one frame's detections through rules, storage, incidents and notifications.

    `frame` is the decoded image (a numpy array) and is optional: an edge device that
    cannot afford to upload frames still gets incidents and alerts, just without
    snapshots. Passing it is strongly preferred - an alert a guard cannot see is an alert
    they have to drive to.
    """
    moment = now or dt.datetime.now(dt.UTC)

    if len(objects) > MAX_OBJECTS_PER_FRAME:
        logger.warning(
            "frame_object_count_truncated",
            extra={"camera_id": str(camera_id), "count": len(objects)},
        )
        objects = sorted(objects, key=lambda o: o.confidence, reverse=True)[
            :MAX_OBJECTS_PER_FRAME
        ]

    rules = await load_rules(session, tenant_id=tenant_id, camera_id=camera_id)

    # `within_active_hours` reads `moment.hour` directly, so it must be given site-local
    # time. Feeding it UTC would shift every overnight rule by the site's offset - a
    # "nobody after 22:00" rule on an Indian site would start firing at 03:30 local and
    # stop at 11:30, which reads as the rule being broken rather than mistimed. The UTC
    # instant is kept for storage; only the evaluator sees the local one.
    local_captured_at = _to_site_time(captured_at, site_timezone)

    # Evaluate everything before writing anything, so the detection row records what the
    # rules actually concluded rather than being written twice.
    fired: list[tuple[Rule, uuid.UUID | None, object]] = []
    rejected: list[str] = []
    for rule, zone_id in rules:
        outcome = evaluate(rule, objects, captured_at=local_captured_at)
        if outcome.fired:
            fired.append((rule, zone_id, outcome))
        else:
            rejected.extend(str(reason) for _, reason in outcome.rejected)

    primary = fired[0] if fired else None
    event_type = primary[0].type_code if primary else "detection.observed"
    confidence = primary[2].best.confidence if primary else (
        max((o.confidence for o in objects), default=0.0)
    )

    detection = DetectionDocument(
        tenant_id=tenant_id,
        site_id=site_id,
        camera_id=camera_id,
        event_type=event_type,
        source_event_id=source_event_id,
        capture_time=captured_at,
        confidence=confidence,
        objects=[
            {
                "class": o.class_name,
                "confidence": round(o.confidence, 4),
                "bbox": [round(v, 5) for v in o.bbox],
            }
            for o in objects
        ],
        roi_id=str(primary[1]) if primary and primary[1] else None,
        model_version_id=model_version_id,
        edge_device_id=edge_device_id,
        correlation_id=correlation_id,
    )
    stored = await record_detection(session, detection)
    is_replay = not stored.created

    result = IngestResult(
        detection_id=str(stored.detection_id),
        duplicate=is_replay,
        rules_evaluated=len(rules),
        rejected_reasons=sorted(set(rejected)),
    )

    if primary is None:
        # Nothing alerted. The detection is still recorded - "what did the camera see at
        # 02:00" is a question people ask, and only having alerting events cannot answer it.
        return result

    if is_replay:
        # A replayed event. The detection upsert already absorbed it; opening or escalating
        # an incident here would turn one real event into repeated alerts.
        logger.info(
            "duplicate_detection_ignored",
            extra={"source_event_id": source_event_id, "camera_id": str(camera_id)},
        )
        return result

    rule, zone_id, outcome = primary
    record = await upsert_incident_from_match(
        session,
        tenant_id=tenant_id,
        site_id=site_id,
        camera_id=camera_id,
        rule=rule,
        detected=outcome.best,
        detection_id=stored.detection_id,
        captured_at=captured_at,
        zone_id=str(zone_id) if zone_id else None,
        correlation_id=correlation_id,
    )

    evidence_captured = False
    if record.created and frame is not None and object_store is not None:
        matched_boxes = {o.bbox for o in outcome.matched}
        try:
            evidence_set = await capture_evidence(
                session,
                object_store,
                tenant_id=tenant_id,
                camera_id=camera_id,
                image=frame,
                capture_time=captured_at,
                incident_id=record.id,
                detection_id=stored.detection_id,
                # Every detected person is masked, not only the ones that alerted -
                # bystanders have the same privacy interest as the subject.
                mask_boxes=[o.bbox for o in objects if o.class_name == "person"],
                annotations=[
                    Annotation(
                        bbox=o.bbox,
                        label=o.class_name,
                        confidence=o.confidence,
                        triggered=o.bbox in matched_boxes,
                    )
                    for o in objects
                ],
            )
            # The annotated variant is what a listing shows, so that is the one the
            # detection points at.
            await attach_evidence_ref(
                session,
                tenant_id=tenant_id,
                detection_id=stored.detection_id,
                evidence_id=str(evidence_set.annotated.evidence_id),
            )
            evidence_captured = True
        except Exception:
            # An alert with no picture beats no alert. Storage problems must not roll back
            # the incident - and this runs inside the caller's transaction, so raising
            # here would discard the detection too.
            logger.exception(
                "evidence_capture_failed", extra={"incident_id": str(record.id)}
            )

    scheduled = 0
    if record.created:
        # Only on creation. A continuing incident folding in its fortieth frame must not
        # re-arm the escalation ladder somebody already acknowledged.
        notification_ids = await schedule_incident_notifications(
            session, incident_id=record.id, correlation_id=correlation_id, now=moment
        )
        scheduled = len(notification_ids)

        # An outbox event, not just the incident_events row `upsert_incident_from_match`
        # already appended - incident_events is this incident's own append-only history,
        # not something anything outside it polls. The realtime relay (WebSocket updates
        # to the Customer CRM) reads outbox_events exclusively, the same as every other
        # cross-service signal in this codebase, so creation has to produce one too or a
        # brand new incident would never reach a live inbox until someone refreshed.
        await record_audit_and_outbox(
            session,
            tenant_id=tenant_id,
            actor_type="pipeline",
            actor_id=rule.type_code,
            action="incident.created",
            outcome="success",
            target_type="incident",
            target_id=str(record.id),
            correlation_id=correlation_id,
            event_type="incident.created.v1",
            event_payload={"incident_id": str(record.id), "previous_status": None},
            aggregate_type="incident",
            aggregate_id=str(record.id),
        )

    return IngestResult(
        detection_id=str(stored.detection_id),
        duplicate=False,
        rules_evaluated=len(rules),
        incident_id=record.id,
        incident_number=record.incident_number,
        incident_created=record.created,
        notifications_scheduled=scheduled,
        evidence_captured=evidence_captured,
        rejected_reasons=result.rejected_reasons,
    )
