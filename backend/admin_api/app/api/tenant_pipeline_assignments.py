"""Admin-side mirror of `tenant_api/app/api/pipeline_assignments.py` — see
`tenant_sites.py`'s module docstring for why this exists and its warning about explicit
`tenant_id` filtering (a `platform_session` sets no tenant scope for RLS to filter by on
`pipeline_assignments`; `pipeline_versions`/`pipelines` themselves are platform-global with
no RLS at all, same as the tenant-facing original, so those two tables need no such
filter).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}", tags=["admin-tenant-pipeline-assignments"])

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "boolean": (bool,),
}


class AssignmentIn(BaseModel):
    pipeline_version_id: uuid.UUID
    tenant_overrides: dict = Field(default_factory=dict)
    runtime_location: str = Field(default="cloud", pattern="^(cloud|edge)$")
    priority: int = Field(default=100, ge=0, le=1000)


class AssignmentOut(BaseModel):
    id: str
    camera_id: str
    pipeline_version_id: str
    pipeline_code: str
    pipeline_version_number: int
    runtime_location: str
    tenant_overrides: dict
    status: str
    priority: int
    effective_from: dt.datetime
    effective_to: dt.datetime | None
    created_at: dt.datetime


_SELECT = """
    SELECT pa.id, pa.camera_id, pa.pipeline_version_id, p.code, pv.version_number,
           pa.runtime_location, pa.tenant_overrides, pa.status::text, pa.priority,
           pa.effective_from, pa.effective_to, pa.created_at
    FROM pipeline_assignments pa
    JOIN pipeline_versions pv ON pv.id = pa.pipeline_version_id
    JOIN pipelines p ON p.id = pv.pipeline_id
"""


def _to_out(row) -> AssignmentOut:
    return AssignmentOut(
        id=str(row[0]), camera_id=str(row[1]), pipeline_version_id=str(row[2]),
        pipeline_code=row[3], pipeline_version_number=row[4], runtime_location=row[5],
        tenant_overrides=row[6] or {}, status=row[7], priority=row[8],
        effective_from=row[9], effective_to=row[10], created_at=row[11],
    )


async def _require_tenant_camera(db: AsyncSession, tenant_id: uuid.UUID, camera_id: uuid.UUID) -> None:
    row = (
        await db.execute(
            text("SELECT 1 FROM cameras WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"),
            {"id": camera_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")


def _validate_overrides(overrides: dict, allowed_schema: dict) -> None:
    unknown = set(overrides) - set(allowed_schema)
    if unknown:
        raise ApiError(
            status_code=422,
            code="pipeline_override_not_allowed",
            message=f"This pipeline version does not allow overriding: {', '.join(sorted(unknown))}.",
            details={"allowed": sorted(allowed_schema)},
        )
    for key, value in overrides.items():
        expected = _TYPE_CHECKS.get(allowed_schema[key])
        if expected is None or not isinstance(value, expected):
            raise ApiError(
                status_code=422,
                code="pipeline_override_wrong_type",
                message=f"'{key}' must be a {allowed_schema[key]}.",
                details={"key": key, "expected_type": allowed_schema[key]},
            )


class AssignableVersionOut(BaseModel):
    pipeline_version_id: str
    pipeline_code: str
    pipeline_name: str
    version_number: int
    use_case: str
    description: str | None
    resource_profile: dict | None
    allowed_overrides_schema: dict


@router.get("/pipelines/assignable", response_model=list[AssignableVersionOut])
async def list_assignable_pipelines(
    tenant_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[AssignableVersionOut]:
    # `tenant_id` is unused here (pipeline_versions is platform-global, not tenant-scoped)
    # but kept on the path for a consistent URL shape with every other admin-tenant route.
    require_permission(context, "tenant.infra.manage")

    rows = (
        await db.execute(
            text(
                """
                SELECT pv.id, p.code, p.name, pv.version_number, p.use_case,
                       p.description, pv.resource_profile, pv.allowed_overrides_schema
                FROM pipeline_versions pv
                JOIN pipelines p ON p.id = pv.pipeline_id
                WHERE pv.state = 'published'
                ORDER BY p.code, pv.version_number DESC
                """
            )
        )
    ).all()
    return [
        AssignableVersionOut(
            pipeline_version_id=str(row[0]), pipeline_code=row[1], pipeline_name=row[2],
            version_number=row[3], use_case=row[4], description=row[5],
            resource_profile=row[6], allowed_overrides_schema=row[7] or {},
        )
        for row in rows
    ]


@router.post("/cameras/{camera_id}/pipeline-assignments", response_model=AssignmentOut, status_code=201)
async def create_assignment(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    body: AssignmentIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> AssignmentOut:
    require_permission(context, "tenant.infra.manage")
    await _require_tenant_camera(db, tenant_id, camera_id)

    version = (
        await db.execute(
            text("SELECT state, allowed_overrides_schema FROM pipeline_versions WHERE id = :id"),
            {"id": body.pipeline_version_id},
        )
    ).first()
    if version is None:
        raise NotFoundError("No such pipeline version.")
    state, allowed_schema = version[0], version[1] or {}
    if state != "published":
        raise ApiError(
            status_code=422,
            code="pipeline_version_not_published",
            message=f"Pipeline version is '{state}', not published. Only a published version can be assigned.",
            details={"state": state},
        )

    _validate_overrides(body.tenant_overrides, allowed_schema)

    try:
        assignment_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO pipeline_assignments
                        (tenant_id, camera_id, pipeline_version_id, runtime_location,
                         tenant_overrides, priority, created_by)
                    VALUES (:tenant_id, :camera_id, :pipeline_version_id, :runtime_location,
                            CAST(:tenant_overrides AS jsonb), :priority, :created_by)
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": tenant_id, "camera_id": camera_id,
                    "pipeline_version_id": body.pipeline_version_id,
                    "runtime_location": body.runtime_location,
                    "tenant_overrides": json.dumps(body.tenant_overrides),
                    "priority": body.priority, "created_by": context.developer_user_id,
                },
            )
        ).scalar_one()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"Camera already has an active pipeline assignment at priority {body.priority}. "
            "Revoke it first, or use a different priority."
        ) from exc

    row = (await db.execute(text(_SELECT + " WHERE pa.id = :id"), {"id": assignment_id})).first()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_pipeline_assignment.create",
        outcome="success",
        target_type="pipeline_assignment",
        target_id=str(assignment_id),
        after_patch={
            "camera_id": str(camera_id), "pipeline_version_id": str(body.pipeline_version_id),
            "priority": body.priority,
        },
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    return _to_out(row)


@router.get("/cameras/{camera_id}/pipeline-assignments", response_model=list[AssignmentOut])
async def list_assignments(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[AssignmentOut]:
    require_permission(context, "tenant.infra.manage")
    await _require_tenant_camera(db, tenant_id, camera_id)

    rows = (
        await db.execute(
            text(_SELECT + " WHERE pa.camera_id = :camera_id AND pa.tenant_id = :tenant_id ORDER BY pa.created_at DESC"),
            {"camera_id": camera_id, "tenant_id": tenant_id},
        )
    ).all()
    return [_to_out(row) for row in rows]


@router.post("/pipeline-assignments/{assignment_id}/revoke", response_model=AssignmentOut)
async def revoke_assignment(
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> AssignmentOut:
    require_permission(context, "tenant.infra.manage")

    existing = (
        await db.execute(
            text(_SELECT + " WHERE pa.id = :id AND pa.tenant_id = :tenant_id"),
            {"id": assignment_id, "tenant_id": tenant_id},
        )
    ).first()
    if existing is None:
        raise NotFoundError("No such pipeline assignment.")
    if existing[7] != "active":
        raise ConflictError(f"Assignment is already '{existing[7]}'.")

    await db.execute(
        text("UPDATE pipeline_assignments SET status = 'revoked', effective_to = now() WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": assignment_id, "tenant_id": tenant_id},
    )
    row = (
        await db.execute(
            text(_SELECT + " WHERE pa.id = :id AND pa.tenant_id = :tenant_id"),
            {"id": assignment_id, "tenant_id": tenant_id},
        )
    ).first()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_pipeline_assignment.revoke",
        outcome="success",
        target_type="pipeline_assignment",
        target_id=str(assignment_id),
        before_patch={"status": "active"},
        after_patch={"status": "revoked"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    return _to_out(row)
