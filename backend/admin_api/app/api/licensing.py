"""License plans and license issuance (docs/05_BACKEND_SCHEMA.md §6.1-6.4, migration
0038). Platform-only: a plan definition and the decision to issue a license are not
tenant-owned actions, gated by a single `license.manage` (mirrors `organization.manage`'s
own read+write simplicity).

Issuing a license does the real work docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §9
describes for entitlement resolution up front, once, rather than at every quota check:
`default_entitlements` from the plan and `entitlement_overrides` from the request are
merged (overrides win) into `license_entitlements` rows, and every `limit_numeric`
entitlement also gets a `quota_ledgers` row - the thing `csense_shared.licensing.quota`
actually locks and increments at resource-creation time.

Issuance also requires a recent step-up verification (TRD-SEC-010, see `mfa.py`) - the one
real high-risk mutation this pass gates.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, get_app_settings, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.step_up_tickets import has_recent_step_up
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin", tags=["admin-licensing"])

# Shared with mfa.py's own STEP_UP_SCOPE - kept as a plain matching string rather than an
# import to avoid a licensing.py <-> mfa.py dependency in either direction; both are
# leaves off the same "admin high-risk action" concept, not a shared object.
STEP_UP_SCOPE = "admin-high-risk"


class EntitlementSpec(BaseModel):
    value_type: str = Field(pattern="^(limit_numeric|boolean|json)$")
    limit_numeric: int | None = None
    enabled_boolean: bool | None = None
    value_json: dict | list | str | int | float | bool | None = None


class LicensePlanIn(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=200)
    license_type: str = Field(min_length=2, max_length=64)
    billing_period: str = Field(pattern="^(quarterly|half_yearly|yearly)$")
    default_entitlements: dict[str, EntitlementSpec] = Field(default_factory=dict)


class LicensePlanOut(BaseModel):
    id: str
    code: str
    name: str
    license_type: str
    billing_period: str
    default_entitlements: dict
    status: str


@router.get("/license-plans", response_model=list[LicensePlanOut])
async def list_license_plans(
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[LicensePlanOut]:
    require_permission(context, "license.manage")
    rows = (
        await db.execute(
            text(
                "SELECT id, code, name, license_type, billing_period, default_entitlements, status "
                "FROM license_plans ORDER BY created_at DESC"
            )
        )
    ).all()
    return [
        LicensePlanOut(
            id=str(r[0]), code=r[1], name=r[2], license_type=r[3], billing_period=r[4],
            default_entitlements=r[5], status=r[6],
        )
        for r in rows
    ]


@router.post("/license-plans", response_model=LicensePlanOut, status_code=201)
async def create_license_plan(
    body: LicensePlanIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> LicensePlanOut:
    require_permission(context, "license.manage")

    existing = (
        await db.execute(text("SELECT 1 FROM license_plans WHERE code = :code"), {"code": body.code})
    ).first()
    if existing is not None:
        raise ConflictError(f"A license plan with code '{body.code}' already exists.")

    default_entitlements = {k: v.model_dump(exclude_none=True) for k, v in body.default_entitlements.items()}
    plan_id = (
        await db.execute(
            text(
                "INSERT INTO license_plans (code, name, license_type, billing_period, default_entitlements) "
                "VALUES (:code, :name, :license_type, :billing_period, CAST(:entitlements AS jsonb)) "
                "RETURNING id"
            ),
            {
                "code": body.code, "name": body.name, "license_type": body.license_type,
                "billing_period": body.billing_period,
                "entitlements": json.dumps(default_entitlements),
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="license_plan.create",
        outcome="success",
        target_type="license_plan",
        target_id=str(plan_id),
        reason=f"Created plan '{body.code}'",
        before_patch=None,
        after_patch={"code": body.code, "billing_period": body.billing_period},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="license_plan.created.v1",
        event_payload={"plan_id": str(plan_id), "code": body.code},
        aggregate_type="license_plan",
        aggregate_id=str(plan_id),
    )

    return LicensePlanOut(
        id=str(plan_id), code=body.code, name=body.name, license_type=body.license_type,
        billing_period=body.billing_period, default_entitlements=default_entitlements, status="active",
    )


class IssueLicenseIn(BaseModel):
    tenant_id: uuid.UUID
    plan_code: str
    expires_at: dt.datetime | None = None
    entitlement_overrides: dict[str, EntitlementSpec] = Field(default_factory=dict)


class LicenseOut(BaseModel):
    id: str
    tenant_id: str
    plan_code: str
    status: str
    starts_at: str
    expires_at: str | None
    entitlements: dict


@router.get("/licenses", response_model=list[LicenseOut])
async def list_licenses(
    tenant_id: uuid.UUID | None = Query(default=None),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[LicenseOut]:
    require_permission(context, "license.manage")

    where = "WHERE l.tenant_id = :tenant_id" if tenant_id else ""
    rows = (
        await db.execute(
            text(
                "SELECT l.id, l.tenant_id, p.code, l.status::text, l.starts_at, l.expires_at "
                "FROM licenses l JOIN license_plans p ON p.id = l.plan_id "
                f"{where} ORDER BY l.created_at DESC LIMIT 200"
            ),
            {"tenant_id": tenant_id} if tenant_id else {},
        )
    ).all()

    out: list[LicenseOut] = []
    for r in rows:
        entitlements = await _load_entitlements(db, license_id=r[0])
        out.append(
            LicenseOut(
                id=str(r[0]), tenant_id=str(r[1]), plan_code=r[2], status=r[3],
                starts_at=r[4].isoformat(), expires_at=r[5].isoformat() if r[5] else None,
                entitlements=entitlements,
            )
        )
    return out


async def _load_entitlements(db: AsyncSession, *, license_id: uuid.UUID) -> dict:
    rows = (
        await db.execute(
            text(
                "SELECT entitlement_code, value_type, limit_numeric, enabled_boolean, value_json "
                "FROM license_entitlements WHERE license_id = :license_id"
            ),
            {"license_id": license_id},
        )
    ).all()
    entitlements: dict = {}
    for code, value_type, limit_numeric, enabled_boolean, value_json in rows:
        spec: dict = {"value_type": value_type}
        if limit_numeric is not None:
            spec["limit_numeric"] = limit_numeric
        if enabled_boolean is not None:
            spec["enabled_boolean"] = enabled_boolean
        if value_json is not None:
            spec["value_json"] = value_json
        entitlements[code] = spec
    return entitlements


@router.post("/licenses", response_model=LicenseOut, status_code=201)
async def issue_license(
    body: IssueLicenseIn,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> LicenseOut:
    require_permission(context, "license.manage")

    # TRD-SEC-010: "High-risk actions require recent MFA/step-up" - issuing a license is
    # a real financial/entitlement action, the concrete one this pass gates (see mfa.py's
    # own docstring for why this endpoint specifically). A valid bearer token alone is not
    # enough; the caller must have verified their second factor within the last few
    # minutes (POST /api/v1/admin/auth/mfa/verify).
    if not await has_recent_step_up(
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.developer_user_id
    ):
        raise ApiError(
            status_code=403, code="step_up_required",
            message="Issuing a license requires a recent MFA verification. "
            "Call POST /api/v1/admin/auth/mfa/verify, then retry.",
        )

    plan_row = (
        await db.execute(
            text("SELECT id, default_entitlements FROM license_plans WHERE code = :code AND status = 'active'"),
            {"code": body.plan_code},
        )
    ).first()
    if plan_row is None:
        raise NotFoundError(f"No active license plan with code '{body.plan_code}'.")
    plan_id, default_entitlements = plan_row

    tenant_row = (
        await db.execute(text("SELECT organization_id FROM tenants WHERE id = :id"), {"id": body.tenant_id})
    ).first()
    if tenant_row is None:
        raise NotFoundError("No such tenant.")
    organization_id = tenant_row[0]

    already = (
        await db.execute(
            text("SELECT 1 FROM licenses WHERE tenant_id = :t AND status IN ('active', 'grace')"),
            {"t": body.tenant_id},
        )
    ).first()
    if already is not None:
        # Upgrade/downgrade/replace is a real, separate flow (superseding the existing
        # license, migrating its quota usage) - not built this pass. Refusing rather than
        # silently creating a second "effective" license is what the schema's own
        # uq_licenses_one_effective_per_tenant index would refuse anyway; this just gives
        # it a clear API-level reason instead of a raw constraint-violation 500.
        raise ConflictError("This tenant already has an active or grace-period license.")

    merged: dict[str, EntitlementSpec] = {
        code: EntitlementSpec(**spec) for code, spec in default_entitlements.items()
    }
    merged.update(body.entitlement_overrides)

    license_id = (
        await db.execute(
            text(
                "INSERT INTO licenses (tenant_id, organization_id, plan_id, expires_at) "
                "VALUES (:tenant_id, :organization_id, :plan_id, :expires_at) RETURNING id, starts_at"
            ),
            {
                "tenant_id": body.tenant_id, "organization_id": organization_id,
                "plan_id": plan_id, "expires_at": body.expires_at,
            },
        )
    ).first()
    license_id, starts_at = license_id[0], license_id[1]

    for code, spec in merged.items():
        await db.execute(
            text(
                "INSERT INTO license_entitlements "
                "(tenant_id, license_id, entitlement_code, value_type, limit_numeric, enabled_boolean, value_json) "
                "VALUES (:tenant_id, :license_id, :code, :value_type, :limit_numeric, :enabled_boolean, "
                "CAST(:value_json AS jsonb))"
            ),
            {
                "tenant_id": body.tenant_id, "license_id": license_id, "code": code,
                "value_type": spec.value_type, "limit_numeric": spec.limit_numeric,
                "enabled_boolean": spec.enabled_boolean,
                "value_json": json.dumps(spec.value_json) if spec.value_json is not None else None,
            },
        )
        if spec.value_type == "limit_numeric" and spec.limit_numeric is not None:
            await db.execute(
                text(
                    "INSERT INTO quota_ledgers "
                    "(tenant_id, license_id, quota_code, period_start, period_end, limit_value) "
                    "VALUES (:tenant_id, :license_id, :code, :period_start, :period_end, :limit_value)"
                ),
                {
                    "tenant_id": body.tenant_id, "license_id": license_id, "code": code,
                    "period_start": starts_at, "period_end": body.expires_at, "limit_value": spec.limit_numeric,
                },
            )

    await record_audit_and_outbox(
        db,
        tenant_id=body.tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="license.issue",
        outcome="success",
        target_type="license",
        target_id=str(license_id),
        reason=f"Issued plan '{body.plan_code}' to tenant {body.tenant_id}",
        before_patch=None,
        after_patch={"plan_code": body.plan_code, "entitlement_codes": list(merged.keys())},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="license.issued.v1",
        event_payload={
            "license_id": str(license_id), "tenant_id": str(body.tenant_id), "plan_code": body.plan_code,
        },
        aggregate_type="license",
        aggregate_id=str(license_id),
    )

    entitlements = await _load_entitlements(db, license_id=license_id)
    return LicenseOut(
        id=str(license_id), tenant_id=str(body.tenant_id), plan_code=body.plan_code,
        status="active", starts_at=starts_at.isoformat(),
        expires_at=body.expires_at.isoformat() if body.expires_at else None,
        entitlements=entitlements,
    )
