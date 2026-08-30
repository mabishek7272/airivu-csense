"""A tenant's own effective license, entitlements, and current quota usage - read-only,
`license.read` (both `tenant_owner` and `tenant_member`, unlike `membership.manage`'s
owner-only tier - this is informational, not access control).

No license is a valid, common state (every tenant that existed before migration 0038, and
any freshly created tenant nobody has assigned a plan to yet) - returned as `null`, not a
404, since "this tenant has no license" is itself the correct, unremarkable answer, not an
error.

**Not filtered to `active`/`grace` any more.** A license that has lapsed into `expired`,
`suspended`, or `revoked` is exactly the thing a tenant most needs to see here - the
whole point of CHECKLIST's "license grace/restriction" item is that a lapse is visible,
not that it makes the endpoint quietly answer `null` again as if nothing had ever been
issued. `csense_shared.licensing.lifecycle.current_license` also means this read is what
lazily flips a stale `active` row to `grace`/`expired` in the first place - the same
"synced on the next read" pattern already used for support-grant expiry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.licensing import current_license
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/license", tags=["license"])


class QuotaUsageOut(BaseModel):
    quota_code: str
    limit_value: int
    reserved_value: int
    consumed_value: int


class EntitlementOut(BaseModel):
    entitlement_code: str
    value_type: str
    limit_numeric: int | None
    enabled_boolean: bool | None
    value_json: object | None


class LicenseOut(BaseModel):
    id: str
    plan_code: str
    plan_name: str
    status: str
    starts_at: str
    expires_at: str | None
    grace_ends_at: str | None
    entitlements: list[EntitlementOut]
    quota_usage: list[QuotaUsageOut]


@router.get("", response_model=LicenseOut | None)
async def get_own_license(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> LicenseOut | None:
    require_permission(context, "license.read")

    found = await current_license(db, tenant_id=context.tenant_id)
    if found is None:
        return None
    license_id, synced_status = found

    row = (
        await db.execute(
            text(
                "SELECT p.code, p.name, l.starts_at, l.expires_at, l.grace_ends_at "
                "FROM licenses l JOIN license_plans p ON p.id = l.plan_id WHERE l.id = :id"
            ),
            {"id": license_id},
        )
    ).first()
    plan_code, plan_name, starts_at, expires_at, grace_ends_at = row
    status = synced_status

    entitlement_rows = (
        await db.execute(
            text(
                "SELECT entitlement_code, value_type, limit_numeric, enabled_boolean, value_json "
                "FROM license_entitlements WHERE license_id = :license_id"
            ),
            {"license_id": license_id},
        )
    ).all()
    entitlements = [
        EntitlementOut(
            entitlement_code=r[0], value_type=r[1], limit_numeric=r[2], enabled_boolean=r[3], value_json=r[4],
        )
        for r in entitlement_rows
    ]

    quota_rows = (
        await db.execute(
            text(
                "SELECT quota_code, limit_value, reserved_value, consumed_value "
                "FROM quota_ledgers WHERE license_id = :license_id "
                "AND period_start <= now() AND (period_end IS NULL OR period_end > now())"
            ),
            {"license_id": license_id},
        )
    ).all()
    quota_usage = [
        QuotaUsageOut(quota_code=r[0], limit_value=r[1], reserved_value=r[2], consumed_value=r[3])
        for r in quota_rows
    ]

    return LicenseOut(
        id=str(license_id), plan_code=plan_code, plan_name=plan_name, status=status,
        starts_at=starts_at.isoformat(), expires_at=expires_at.isoformat() if expires_at else None,
        grace_ends_at=grace_ends_at.isoformat() if grace_ends_at else None,
        entitlements=entitlements, quota_usage=quota_usage,
    )
