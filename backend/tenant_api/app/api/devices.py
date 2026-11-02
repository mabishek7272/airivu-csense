"""Signed-in phones/tablets for this tenant, and the limit on them.

Lets an account owner see which devices hold a slot (`mobile_device.count` on the
licence) and remove one, which frees the slot and signs that device out. The plan itself
is assigned by the platform; nothing here changes a limit.
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from app.services.sessions import revoke_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import NotFoundError
from csense_shared.licensing.limits import (
    MOBILE_DEVICE_LIMIT,
    WEB_SESSION_LIMIT,
    entitlement_limit,
)
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/devices", tags=["devices"])


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str | None = None
    platform: str | None = None
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class DevicesOut(BaseModel):
    device_limit: int | None
    web_session_limit: int | None
    devices: list[DeviceOut]


@router.get("", response_model=DevicesOut)
async def list_devices(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DevicesOut:
    require_permission(context, "membership.read")
    rows = (
        await db.execute(
            text(
                "SELECT id, name, platform, first_seen_at, last_seen_at FROM mobile_devices "
                "WHERE revoked_at IS NULL ORDER BY last_seen_at DESC"
            )
        )
    ).all()
    return DevicesOut(
        device_limit=await entitlement_limit(db, context.tenant_id, MOBILE_DEVICE_LIMIT),
        web_session_limit=await entitlement_limit(db, context.tenant_id, WEB_SESSION_LIMIT),
        devices=[
            DeviceOut(id=r[0], name=r[1], platform=r[2], first_seen_at=r[3], last_seen_at=r[4])
            for r in rows
        ],
    )


@router.delete("/{device_pk}", status_code=204)
async def remove_device(
    device_pk: uuid.UUID,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "membership.manage")
    row = (
        await db.execute(
            text("SELECT name, session_id FROM mobile_devices WHERE id = :id AND revoked_at IS NULL"),
            {"id": device_pk},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such device.")

    await db.execute(
        text("UPDATE mobile_devices SET revoked_at = now(), session_id = NULL WHERE id = :id"),
        {"id": device_pk},
    )
    if row[1]:
        await revoke_session(request.app.state.redis, request.app.state.settings, session_id=row[1])

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="device.remove",
        outcome="success",
        target_type="mobile_device",
        target_id=str(device_pk),
        after_patch={"name": row[0]},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
