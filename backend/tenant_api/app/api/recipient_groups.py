"""Recipient groups — who gets told when an incident opens, and how.

This and `notification_policies.py` are the last piece of "configure everything from the
UI, no SQL" for alerting: `recipient_groups` and `notification_policies` have existed
since the dispatcher was built, but nothing has ever let a tenant edit them except a
direct INSERT.

A member's `active_schedule` is their own quiet hours, not the policy's or the step's -
two people in the same group can be reached at different times of night, which is why it
lives here rather than on the escalation step. The dispatcher's own reasoning for it is in
migration 0015: "a fire alarm ignores them; a housekeeping alert should not" - `high` and
`critical` notifications always go through regardless of what a member set.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext
from csense_shared.timezones import is_usable_timezone

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/tenant/notifications/recipient-groups", tags=["notifications"]
)

# Mirrors csense_shared.notifications.providers.Channel. Not imported directly: that enum
# is what the dispatcher trusts already-validated data to be, and duplicating the values
# here keeps this module's validation independent of a change there having to be noticed.
CHANNELS = ("in_app", "email", "whatsapp", "sms", "web_push", "webhook")


class QuietHoursIn(BaseModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if not is_usable_timezone(value):
            raise ValueError(f"'{value}' is not a recognised timezone.")
        return value

    @model_validator(mode="after")
    def _not_zero_length(self):
        if self.start == self.end:
            raise ValueError(
                "Start and end are the same time - that is a zero-length window, which "
                "quiets nothing. Set them apart, or clear the schedule entirely."
            )
        return self


def _clean_channels(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for v in values:
        if v not in CHANNELS:
            raise ValueError(f"'{v}' is not a channel this platform supports.")
        if v not in cleaned:
            cleaned.append(v)
    if not cleaned:
        raise ValueError("Choose at least one channel - with none, this person is never reached.")
    return cleaned


class MemberIn(BaseModel):
    user_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")
    channels: list[str] = Field(default_factory=lambda: ["email"], max_length=6)
    active_schedule: QuietHoursIn | None = None

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, values: list[str]) -> list[str]:
        return _clean_channels(values)

    @model_validator(mode="after")
    def _has_an_address(self):
        # Mirrors ck_recipient_has_an_address (migration 0015) - reported here so a
        # blank form is refused with a reason, not a raw constraint-violation 500.
        if not (self.user_id or self.email or self.phone_e164):
            raise ValueError(
                "Give an email or phone number, or link an existing user - a recipient "
                "with none of those can never be reached."
            )
        return self


class MemberPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")
    channels: list[str] | None = Field(default=None, max_length=6)
    active_schedule: QuietHoursIn | None = None
    clear_schedule: bool = False
    status: str | None = Field(default=None, pattern="^(active|disabled)$")

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, values: list[str] | None) -> list[str] | None:
        return _clean_channels(values) if values is not None else None


class MemberOut(BaseModel):
    id: uuid.UUID
    recipient_group_id: uuid.UUID
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    email: str | None = None
    phone_e164: str | None = None
    channels: list[str] = []
    active_schedule: dict | None = None
    status: str
    created_at: str


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    status: str
    member_count: int
    created_at: str


async def _referencing_policy(
    db: AsyncSession, *, group_id: uuid.UUID
) -> str | None:
    """The name of an active policy whose published escalation still names this group, if
    any - checked before a group is deleted out from under it."""
    rows = (
        await db.execute(
            text(
                """
                SELECT p.name, v.definition_json
                FROM notification_policies p
                JOIN notification_policy_versions v ON v.id = p.active_version_id
                WHERE p.status = 'active'
                """
            )
        )
    ).all()
    needle = str(group_id)
    for name, definition in rows:
        steps = (definition or {}).get("steps") or []
        for step in steps:
            if needle in (step.get("recipient_group_ids") or []):
                return name
    return None


@router.get("", response_model=list[GroupOut])
async def list_groups(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[GroupOut]:
    require_permission(context, "notification.read")
    rows = (
        await db.execute(
            text(
                """
                SELECT g.id, g.name, g.description, g.status, g.created_at,
                       (SELECT count(*) FROM recipient_group_members m
                         WHERE m.recipient_group_id = g.id AND m.status = 'active')
                FROM recipient_groups g
                ORDER BY g.name
                """
            )
        )
    ).all()
    return [
        GroupOut(
            id=r[0], name=r[1], description=r[2], status=r[3],
            created_at=r[4].isoformat(), member_count=r[5],
        )
        for r in rows
    ]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> GroupOut:
    require_permission(context, "notification.manage")
    clash = (
        await db.execute(
            text("SELECT 1 FROM recipient_groups WHERE tenant_id = :t AND name = :n"),
            {"t": context.tenant_id, "n": body.name},
        )
    ).first()
    if clash:
        raise ApiError(
            status_code=409, code="group_name_taken",
            message=f"A recipient group named '{body.name}' already exists.",
        )
    row = (
        await db.execute(
            text(
                "INSERT INTO recipient_groups (tenant_id, name, description) "
                "VALUES (:t, :n, :d) RETURNING id, name, description, status, created_at"
            ),
            {"t": context.tenant_id, "n": body.name, "d": body.description},
        )
    ).first()
    logger.info("recipient_group_created", extra={"group_id": str(row[0])})
    return GroupOut(
        id=row[0], name=row[1], description=row[2], status=row[3],
        created_at=row[4].isoformat(), member_count=0,
    )


async def _load_group_row(db: AsyncSession, group_id: uuid.UUID):
    row = (
        await db.execute(
            text(
                "SELECT id, name, description, status, created_at FROM recipient_groups "
                "WHERE id = :id"
            ),
            {"id": group_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such recipient group.")
    return row


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: uuid.UUID,
    body: GroupPatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> GroupOut:
    require_permission(context, "notification.manage")
    await _load_group_row(db, group_id)

    changes = body.model_dump(exclude_unset=True)
    if changes:
        assignments = ", ".join(f"{f} = :{f}" for f in changes)
        await db.execute(
            text(
                f"UPDATE recipient_groups SET {assignments}, updated_at = now(), "
                "version = version + 1 WHERE id = :id"
            ),
            {"id": group_id, **changes},
        )
        logger.info(
            "recipient_group_updated",
            extra={"group_id": str(group_id), "fields": sorted(changes)},
        )

    row = (
        await db.execute(
            text(
                """
                SELECT g.id, g.name, g.description, g.status, g.created_at,
                       (SELECT count(*) FROM recipient_group_members m
                         WHERE m.recipient_group_id = g.id AND m.status = 'active')
                FROM recipient_groups g WHERE g.id = :id
                """
            ),
            {"id": group_id},
        )
    ).first()
    return GroupOut(
        id=row[0], name=row[1], description=row[2], status=row[3],
        created_at=row[4].isoformat(), member_count=row[5],
    )


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "notification.manage")
    await _load_group_row(db, group_id)

    blocking_policy = await _referencing_policy(db, group_id=group_id)
    if blocking_policy:
        raise ApiError(
            status_code=409,
            code="group_in_use",
            message=(
                f"'{blocking_policy}' still notifies this group in its published "
                "escalation. Remove the group from that policy first, or it would go "
                "silent for whoever this step was supposed to reach."
            ),
        )

    await db.execute(text("DELETE FROM recipient_groups WHERE id = :id"), {"id": group_id})
    logger.info("recipient_group_deleted", extra={"group_id": str(group_id)})


def _to_member(row) -> MemberOut:
    return MemberOut(
        id=row[0], recipient_group_id=row[1], user_id=row[2], display_name=row[3],
        email=row[4], phone_e164=row[5], channels=row[6] or [], active_schedule=row[7],
        status=row[8], created_at=row[9].isoformat(),
    )


_MEMBER_SELECT = (
    "SELECT id, recipient_group_id, user_id, display_name, email, phone_e164, "
    "channels, active_schedule, status, created_at FROM recipient_group_members"
)


@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[MemberOut]:
    require_permission(context, "notification.read")
    await _load_group_row(db, group_id)
    rows = (
        await db.execute(
            text(f"{_MEMBER_SELECT} WHERE recipient_group_id = :g ORDER BY created_at"),
            {"g": group_id},
        )
    ).all()
    return [_to_member(r) for r in rows]


@router.post("/{group_id}/members", response_model=MemberOut, status_code=201)
async def add_member(
    group_id: uuid.UUID,
    body: MemberIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> MemberOut:
    require_permission(context, "notification.manage")
    await _load_group_row(db, group_id)

    if body.user_id is not None:
        user = (
            await db.execute(
                text(
                    "SELECT 1 FROM memberships WHERE tenant_id = :t AND user_id = :u "
                    "AND status = 'active'"
                ),
                {"t": context.tenant_id, "u": body.user_id},
            )
        ).first()
        if user is None:
            raise ApiError(
                status_code=422, code="user_not_a_member",
                message="That user is not an active member of this tenant.",
            )

    row = (
        await db.execute(
            text(
                """
                INSERT INTO recipient_group_members
                    (tenant_id, recipient_group_id, user_id, display_name, email,
                     phone_e164, channels, active_schedule)
                VALUES (:tenant_id, :group_id, :user_id, :display_name, :email,
                        :phone_e164, CAST(:channels AS jsonb), CAST(:schedule AS jsonb))
                RETURNING id, recipient_group_id, user_id, display_name, email,
                          phone_e164, channels, active_schedule, status, created_at
                """
            ),
            {
                "tenant_id": context.tenant_id, "group_id": group_id,
                "user_id": body.user_id, "display_name": body.display_name,
                "email": body.email, "phone_e164": body.phone_e164,
                "channels": _json(body.channels),
                "schedule": _json(body.active_schedule.model_dump()) if body.active_schedule else None,
            },
        )
    ).first()
    logger.info("recipient_added", extra={"group_id": str(group_id), "member_id": str(row[0])})
    return _to_member(row)


async def _load_member_row(db: AsyncSession, group_id: uuid.UUID, member_id: uuid.UUID):
    row = (
        await db.execute(
            text(f"{_MEMBER_SELECT} WHERE id = :id AND recipient_group_id = :g"),
            {"id": member_id, "g": group_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such recipient.")
    return row


@router.patch("/{group_id}/members/{member_id}", response_model=MemberOut)
async def update_member(
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberPatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> MemberOut:
    require_permission(context, "notification.manage")
    await _load_member_row(db, group_id, member_id)

    changes = body.model_dump(exclude_unset=True, exclude={"clear_schedule", "active_schedule"})
    assignments = [f"{f} = :{f}" for f in changes]
    params: dict = dict(changes)

    if body.clear_schedule:
        assignments.append("active_schedule = NULL")
    elif "active_schedule" in body.model_fields_set and body.active_schedule is not None:
        assignments.append("active_schedule = CAST(:schedule AS jsonb)")
        params["schedule"] = _json(body.active_schedule.model_dump())

    if "channels" in params:
        params["channels"] = _json(params["channels"])

    if assignments:
        await db.execute(
            text(
                f"UPDATE recipient_group_members SET {', '.join(assignments)} "
                "WHERE id = :id"
            ),
            {"id": member_id, **params},
        )
        logger.info(
            "recipient_updated",
            extra={"member_id": str(member_id), "fields": sorted(changes) or ["active_schedule"]},
        )

    return _to_member(await _load_member_row(db, group_id, member_id))


@router.delete("/{group_id}/members/{member_id}", status_code=204)
async def remove_member(
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "notification.manage")
    await _load_member_row(db, group_id, member_id)
    await db.execute(text("DELETE FROM recipient_group_members WHERE id = :id"), {"id": member_id})
    logger.info("recipient_removed", extra={"member_id": str(member_id)})


def _json(value) -> str:
    import json

    return json.dumps(value)
