"""Notification policies — which escalation ladder applies to which incident.

The other half of "configure alerting from the UI, no SQL" (see recipient_groups.py).

A policy's versions are immutable and append-only (migration 0015): publishing edits
never overwrites what already produced a notification, so "why was I called at 3am" stays
answerable after the policy has since been changed. `POST .../versions` is therefore the
only way a policy's escalation ladder is ever written - a policy with no published version
exists but reaches nobody, which is a deliberate, free "draft" state: `resolve_policy`
inner-joins to `active_version_id`, so a policy without one is invisible to it.

Quiet hours are **not** configured here. They belong to each recipient, not to a policy or
a step - see recipient_groups.py - because two people in the same escalation step can
legitimately want to be reached at different hours.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.notifications.policies import MAX_DELAY_SECONDS, MAX_STEPS
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/notifications/policies", tags=["notifications"])

SEVERITIES = ("info", "low", "medium", "high", "critical")
CHANNELS = ("in_app", "email", "whatsapp", "sms", "web_push", "webhook")


class StepIn(BaseModel):
    level: int = Field(ge=0, le=MAX_STEPS - 1)
    delay_seconds: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)
    channels: list[str] = Field(min_length=1, max_length=6)
    recipient_group_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)

    @field_validator("channels")
    @classmethod
    def _known_channels(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for v in values:
            if v not in CHANNELS:
                raise ValueError(f"'{v}' is not a channel this platform supports.")
            if v not in cleaned:
                cleaned.append(v)
        return cleaned

    @field_validator("recipient_group_ids")
    @classmethod
    def _dedupe_groups(cls, values: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(values))


class PublishIn(BaseModel):
    steps: list[StepIn] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _levels_are_unique(self):
        levels = [s.level for s in self.steps]
        if len(levels) != len(set(levels)):
            raise ValueError(
                "Two steps share the same level - the database can only keep one "
                "notification per incident per level, so the second would silently "
                "vanish rather than send."
            )
        return self


def _clean_severities(values: list[str]) -> list[str]:
    cleaned = []
    for v in values:
        if v not in SEVERITIES:
            raise ValueError(f"'{v}' is not a recognised severity.")
        if v not in cleaned:
            cleaned.append(v)
    return cleaned


class PolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Empty means "every severity" / "every event type" - the same convention
    # PolicyDefinition.applies_to already uses for an empty severities set.
    severities: list[str] = Field(default_factory=list, max_length=len(SEVERITIES))
    type_codes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("severities")
    @classmethod
    def _validate_severities(cls, values: list[str]) -> list[str]:
        return _clean_severities(values)

    @field_validator("type_codes")
    @classmethod
    def _clean_types(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


class PolicyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    severities: list[str] | None = None
    type_codes: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")

    @field_validator("severities")
    @classmethod
    def _validate_severities(cls, values: list[str] | None) -> list[str] | None:
        return _clean_severities(values) if values is not None else None


class VersionOut(BaseModel):
    version_number: int
    steps: list[dict]
    published_at: str


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    severities: list[str]
    type_codes: list[str]
    status: str
    active_version: VersionOut | None = None
    created_at: str


_SELECT = """
    SELECT p.id, p.name, p.event_filter, p.status, p.created_at,
           v.version_number, v.definition_json, v.published_at
    FROM notification_policies p
    LEFT JOIN notification_policy_versions v ON v.id = p.active_version_id
"""


def _to_policy(row) -> PolicyOut:
    event_filter = row[2] or {}
    active_version = None
    if row[5] is not None:
        active_version = VersionOut(
            version_number=row[5],
            steps=(row[6] or {}).get("steps") or [],
            published_at=row[7].isoformat(),
        )
    return PolicyOut(
        id=row[0], name=row[1],
        severities=event_filter.get("severities") or [],
        type_codes=event_filter.get("type_codes") or [],
        status=row[3], active_version=active_version, created_at=row[4].isoformat(),
    )


@router.get("", response_model=list[PolicyOut])
async def list_policies(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[PolicyOut]:
    require_permission(context, "notification.read")
    rows = (await db.execute(text(f"{_SELECT} ORDER BY p.name"))).all()
    return [_to_policy(r) for r in rows]


@router.post("", response_model=PolicyOut, status_code=201)
async def create_policy(
    body: PolicyIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> PolicyOut:
    require_permission(context, "notification.manage")
    clash = (
        await db.execute(
            text("SELECT 1 FROM notification_policies WHERE tenant_id = :t AND name = :n"),
            {"t": context.tenant_id, "n": body.name},
        )
    ).first()
    if clash:
        raise ApiError(
            status_code=409, code="policy_name_taken",
            message=f"A notification policy named '{body.name}' already exists.",
        )
    event_filter = {"severities": body.severities, "type_codes": body.type_codes}
    policy_id = (
        await db.execute(
            text(
                "INSERT INTO notification_policies (tenant_id, name, event_filter) "
                "VALUES (:t, :n, CAST(:f AS jsonb)) RETURNING id"
            ),
            {"t": context.tenant_id, "n": body.name, "f": json.dumps(event_filter)},
        )
    ).scalar_one()
    logger.info("notification_policy_created", extra={"policy_id": str(policy_id)})
    return await _load(db, policy_id)


async def _load(db: AsyncSession, policy_id: uuid.UUID) -> PolicyOut:
    row = (await db.execute(text(f"{_SELECT} WHERE p.id = :id"), {"id": policy_id})).first()
    if row is None:
        raise NotFoundError("No such notification policy.")
    return _to_policy(row)


async def _exists(db: AsyncSession, policy_id: uuid.UUID) -> None:
    row = (
        await db.execute(
            text("SELECT 1 FROM notification_policies WHERE id = :id"), {"id": policy_id}
        )
    ).first()
    if row is None:
        raise NotFoundError("No such notification policy.")


@router.patch("/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyPatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> PolicyOut:
    require_permission(context, "notification.manage")
    await _exists(db, policy_id)

    changes = body.model_dump(exclude_unset=True)
    if "severities" in changes or "type_codes" in changes:
        current = (
            await db.execute(
                text("SELECT event_filter FROM notification_policies WHERE id = :id"),
                {"id": policy_id},
            )
        ).scalar_one()
        merged = dict(current or {})
        if "severities" in changes:
            merged["severities"] = changes.pop("severities")
        if "type_codes" in changes:
            merged["type_codes"] = changes.pop("type_codes")
        changes["event_filter"] = json.dumps(merged)

    if changes:
        assignments = []
        for field in changes:
            cast = " = CAST(:event_filter AS jsonb)" if field == "event_filter" else f" = :{field}"
            assignments.append(f"{field}{cast}")
        await db.execute(
            text(
                f"UPDATE notification_policies SET {', '.join(assignments)}, "
                "updated_at = now(), version = version + 1 WHERE id = :id"
            ),
            {"id": policy_id, **changes},
        )
        logger.info(
            "notification_policy_updated",
            extra={"policy_id": str(policy_id), "fields": sorted(changes)},
        )
    return await _load(db, policy_id)


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "notification.manage")
    await _exists(db, policy_id)

    sent = (
        await db.execute(
            text(
                """
                SELECT count(*) FROM notifications n
                JOIN notification_policy_versions v ON v.id = n.policy_version_id
                WHERE v.policy_id = :id
                """
            ),
            {"id": policy_id},
        )
    ).scalar_one()
    if sent:
        raise ApiError(
            status_code=409,
            code="policy_has_history",
            message=(
                f"This policy has produced {sent} notification(s) - deleting it would "
                "break that history's audit trail. Disable it instead; a disabled policy "
                "is skipped by every future incident."
            ),
        )

    await db.execute(text("DELETE FROM notification_policies WHERE id = :id"), {"id": policy_id})
    logger.info("notification_policy_deleted", extra={"policy_id": str(policy_id)})


@router.post("/{policy_id}/versions", response_model=PolicyOut, status_code=201)
async def publish_version(
    policy_id: uuid.UUID,
    body: PublishIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> PolicyOut:
    """Publishes a new, immutable version and makes it the active one.

    Every recipient group a step names is checked against this tenant's own groups first -
    a typo'd or stale id would otherwise sit unnoticed in a published policy, silently
    reaching nobody the day it matters.
    """
    require_permission(context, "notification.manage")
    await _exists(db, policy_id)

    all_group_ids = {gid for step in body.steps for gid in step.recipient_group_ids}
    if all_group_ids:
        found = set(
            (
                await db.execute(
                    text(
                        "SELECT id FROM recipient_groups WHERE tenant_id = :t "
                        "AND id = ANY(:ids)"
                    ),
                    {"t": context.tenant_id, "ids": list(all_group_ids)},
                )
            ).scalars()
        )
        missing = all_group_ids - found
        if missing:
            raise ApiError(
                status_code=422,
                code="unknown_recipient_group",
                message=(
                    f"{len(missing)} recipient group(s) in this escalation do not exist "
                    "- a published step naming one would silently reach nobody."
                ),
            )

    definition = {
        "steps": [
            {
                "level": s.level,
                "delay_seconds": s.delay_seconds,
                "channels": s.channels,
                "recipient_group_ids": [str(g) for g in s.recipient_group_ids],
            }
            for s in sorted(body.steps, key=lambda s: s.level)
        ],
    }
    # Carries the policy's own event filter forward so parse_definition (which reads
    # `severities` off the same document at dispatch time) keeps working unchanged.
    current_filter = (
        await db.execute(
            text("SELECT event_filter FROM notification_policies WHERE id = :id"),
            {"id": policy_id},
        )
    ).scalar_one()
    definition["severities"] = (current_filter or {}).get("severities") or []

    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()

    next_number = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 "
                "FROM notification_policy_versions WHERE policy_id = :id"
            ),
            {"id": policy_id},
        )
    ).scalar_one()

    version_id = (
        await db.execute(
            text(
                """
                INSERT INTO notification_policy_versions
                    (tenant_id, policy_id, version_number, definition_json,
                     definition_sha256, published_by)
                VALUES (:tenant_id, :policy_id, :version_number, CAST(:definition AS jsonb),
                        :digest, :published_by)
                RETURNING id
                """
            ),
            {
                "tenant_id": context.tenant_id, "policy_id": policy_id,
                "version_number": next_number, "definition": json.dumps(definition),
                "digest": digest, "published_by": context.user_id,
            },
        )
    ).scalar_one()
    await db.execute(
        text("UPDATE notification_policies SET active_version_id = :v WHERE id = :id"),
        {"v": version_id, "id": policy_id},
    )
    logger.info(
        "notification_policy_version_published",
        extra={"policy_id": str(policy_id), "version_number": next_number},
    )
    return await _load(db, policy_id)
