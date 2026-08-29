"""Pipeline assignment: pointing one of a tenant's own cameras at a published pipeline
version (TRD `POST /api/v1/tenant/cameras/{id}/pipeline-assignments`, named there
explicitly; SCH §8.8).

**This records intent, not execution.** Nothing in this codebase yet pulls a camera's
stream and runs the assigned pipeline against it - that's a materially different, larger
piece of work (a gateway device pulling RTSP, or the cloud doing so, and calling AI
Runtime's `/infer` continuously) than the registry this assignment lives in. An assignment
here is real, durable configuration - "this camera should run this pipeline version" - the
same way a `pipeline_version` in `draft` state is real even before it's ever assigned
anywhere. Executing it is future work, tracked in CHECKLIST.md, not silently implied by
this endpoint existing.

`pipeline_versions` itself is platform-global (no RLS, same as `models`) - a tenant reads
it here only to validate an assignment target, never to list or browse the catalogue; that
stays an Admin/Console concern (`GET /api/v1/admin/pipelines`).
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

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant", tags=["pipeline-assignments"])

# JSON-type-name -> the Python types that satisfy it. A simple key/type check against a
# version's `allowed_overrides_schema`, not full JSON-Schema-draft validation - see
# pipelines.py's own docstring for why that's a deliberate stage-1 simplification.
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


async def _require_own_camera(db: AsyncSession, camera_id: uuid.UUID) -> None:
    # RLS already scopes this - a missing row means either "no such camera" or "not
    # yours", and those must stay indistinguishable (cameras.py's own create_camera does
    # the identical check against sites for the identical reason).
    row = (await db.execute(text("SELECT 1 FROM cameras WHERE id = :id AND deleted_at IS NULL"), {"id": camera_id})).first()
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
        # An unrecognised schema type name means the version was published with a typo
        # in its own allowed_overrides_schema - refuse rather than silently accept
        # anything for that key.
        if expected is None or not isinstance(value, expected):
            raise ApiError(
                status_code=422,
                code="pipeline_override_wrong_type",
                message=f"'{key}' must be a {allowed_schema[key]}.",
                details={"key": key, "expected_type": allowed_schema[key]},
            )


@router.post("/cameras/{camera_id}/pipeline-assignments", response_model=AssignmentOut, status_code=201)
async def create_assignment(
    camera_id: uuid.UUID,
    body: AssignmentIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> AssignmentOut:
    require_permission(context, "pipeline.assign")
    await _require_own_camera(db, camera_id)

    version = (
        await db.execute(
            text(
                "SELECT state, allowed_overrides_schema FROM pipeline_versions WHERE id = :id"
            ),
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
                    "tenant_id": context.tenant_id, "camera_id": camera_id,
                    "pipeline_version_id": body.pipeline_version_id,
                    "runtime_location": body.runtime_location,
                    "tenant_overrides": json.dumps(body.tenant_overrides),
                    "priority": body.priority, "created_by": context.user_id,
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
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        action="pipeline.assignment.create",
        outcome="success",
        target_type="pipeline_assignment",
        target_id=str(assignment_id),
        after_patch={
            "camera_id": str(camera_id), "pipeline_version_id": str(body.pipeline_version_id),
            "priority": body.priority,
        },
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="pipeline.assignment.changed.v1",
        event_payload={
            "assignment_id": str(assignment_id), "camera_id": str(camera_id),
            "pipeline_version_id": str(body.pipeline_version_id), "status": "active",
        },
        aggregate_type="pipeline_assignment",
        aggregate_id=str(assignment_id),
    )

    return _to_out(row)


@router.get("/cameras/{camera_id}/pipeline-assignments", response_model=list[AssignmentOut])
async def list_assignments(
    camera_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[AssignmentOut]:
    require_permission(context, "pipeline.assign")
    await _require_own_camera(db, camera_id)

    rows = (
        await db.execute(
            text(_SELECT + " WHERE pa.camera_id = :camera_id ORDER BY pa.created_at DESC"),
            {"camera_id": camera_id},
        )
    ).all()
    return [_to_out(row) for row in rows]


@router.post("/pipeline-assignments/{assignment_id}/revoke", response_model=AssignmentOut)
async def revoke_assignment(
    assignment_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> AssignmentOut:
    require_permission(context, "pipeline.assign")

    existing = (await db.execute(text(_SELECT + " WHERE pa.id = :id"), {"id": assignment_id})).first()
    if existing is None:
        raise NotFoundError("No such pipeline assignment.")
    if existing[7] != "active":
        raise ConflictError(f"Assignment is already '{existing[7]}'.")

    await db.execute(
        text("UPDATE pipeline_assignments SET status = 'revoked', effective_to = now() WHERE id = :id"),
        {"id": assignment_id},
    )
    row = (await db.execute(text(_SELECT + " WHERE pa.id = :id"), {"id": assignment_id})).first()

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        action="pipeline.assignment.revoke",
        outcome="success",
        target_type="pipeline_assignment",
        target_id=str(assignment_id),
        before_patch={"status": "active"},
        after_patch={"status": "revoked"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="pipeline.assignment.changed.v1",
        event_payload={"assignment_id": str(assignment_id), "status": "revoked"},
        aggregate_type="pipeline_assignment",
        aggregate_id=str(assignment_id),
    )

    return _to_out(row)
