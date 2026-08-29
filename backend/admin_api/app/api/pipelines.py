"""Pipeline registry endpoints (TRD §16, SCH §8.4-8.5, §8.8's own naming: `pipeline.publish`
and `pipeline.assign`): `/api/v1/admin/pipelines`, `/pipelines/{id}/versions`,
`/pipeline-versions/{id}/publish`, `/pipeline-versions/{id}/deprecate`.

Platform-only, mirroring models.py exactly - a pipeline definition is not tenant-owned
data any more than a model artifact is. Tenants reach a pipeline only indirectly, through
`pipeline_assignments` (backend/tenant_api/app/api/pipeline_assignments.py).

**Only one stage type is interpreted anywhere in this codebase today: `infer`.** The TRD's
own pipeline diagram (§16) has more (preprocess, filter, tracking...), and
`definition_json` is shaped to hold them later, but nothing executes a pipeline yet at
all - see the module docstring on `pipeline_assignments` for why that's deliberately out
of scope this pass. So version creation validates exactly one thing for real: that the
named model exists and has a version in a deployable state, the same
`DEPLOYABLE_STATES` bar models.py's own promote endpoint already uses.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.db.models import Model, ModelVersion, Pipeline, PipelineVersion
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin", tags=["admin-pipelines"])

# Same bar as models.py's own promote endpoint: a version in one of these states is one a
# pipeline may actually be pointed at.
DEPLOYABLE_STATES = {"validated", "staging", "production"}

VERSION_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"published"},
    "published": {"deprecated"},
    "deprecated": set(),
}


class CreatePipelineRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=200)
    use_case: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    owner_team: str | None = Field(default=None, max_length=100)


class InferStage(BaseModel):
    """The one stage type any runtime actually interprets today - see the module
    docstring. `min_model_state` is recorded but not enforced against a ladder in this
    pass; presence in DEPLOYABLE_STATES is the actual gate."""

    type: Literal["infer"]
    model_name: str
    min_model_state: str = "validated"


class CreateVersionRequest(BaseModel):
    stages: list[InferStage] = Field(min_length=1, max_length=1)
    schema_version: int = Field(default=1, ge=1)
    # Simple key -> JSON-type-name map ("string" | "number" | "boolean"), checked against
    # a tenant's tenant_overrides at assignment time - not full JSON-Schema-draft
    # validation, a stage-1 simplification recorded in the plan.
    allowed_overrides_schema: dict[str, str] = Field(default_factory=dict)
    runtime_target: str = Field(default="cloud")
    resource_profile: dict | None = None

    @field_validator("allowed_overrides_schema")
    @classmethod
    def _known_override_types(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = {v for v in value.values() if v not in ("string", "number", "boolean")}
        if unknown:
            raise ValueError(
                f"allowed_overrides_schema values must be 'string', 'number', or "
                f"'boolean' - got {sorted(unknown)}"
            )
        return value


class TransitionRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=500, description="Recorded in the audit trail")


class PipelineVersionOut(BaseModel):
    pipeline_id: str
    code: str
    name: str
    use_case: str
    pipeline_status: str
    # Null when a pipeline has no versions yet - a pipeline is created before its first
    # version exists, and the list has to be able to show that rather than the pipeline
    # simply not appearing until someone adds one.
    version_id: str | None = None
    version_number: int | None = None
    state: str | None = None
    definition_json: dict | None = None
    definition_sha256: str | None = None
    runtime_target: str | None = None
    allowed_overrides_schema: dict | None = None
    created_at: dt.datetime | None = None
    approved_at: dt.datetime | None = None


def _definition_sha256(definition_json: dict) -> str:
    canonical = json.dumps(definition_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _model_has_deployable_version(db: AsyncSession, model_name: str) -> bool:
    result = await db.execute(
        select(ModelVersion.id)
        .join(Model, Model.id == ModelVersion.model_id)
        .where(Model.name == model_name, ModelVersion.state.in_(DEPLOYABLE_STATES))
        .limit(1)
    )
    return result.first() is not None


def _to_out(pipeline: Pipeline, version: PipelineVersion | None) -> PipelineVersionOut:
    base = dict(
        pipeline_id=str(pipeline.id),
        code=pipeline.code,
        name=pipeline.name,
        use_case=pipeline.use_case,
        pipeline_status=pipeline.status,
    )
    if version is None:
        return PipelineVersionOut(**base)
    return PipelineVersionOut(
        **base,
        version_id=str(version.id),
        version_number=version.version_number,
        state=version.state,
        definition_json=version.definition_json,
        definition_sha256=version.definition_sha256,
        runtime_target=version.runtime_target,
        allowed_overrides_schema=version.allowed_overrides_schema,
        created_at=version.created_at,
        approved_at=version.approved_at,
    )


@router.post("/pipelines", response_model=dict)
async def create_pipeline(
    body: CreatePipelineRequest,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> dict:
    require_permission(context, "pipeline.manage")

    existing = await db.execute(select(Pipeline.id).where(Pipeline.code == body.code))
    if existing.first() is not None:
        raise ConflictError(f"A pipeline with code '{body.code}' already exists.")

    pipeline = Pipeline(
        code=body.code, name=body.name, use_case=body.use_case,
        description=body.description, owner_team=body.owner_team,
    )
    db.add(pipeline)
    await db.flush()

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="pipeline.create",
        outcome="success",
        target_type="pipeline",
        target_id=str(pipeline.id),
        after_patch={"code": body.code, "name": body.name, "use_case": body.use_case},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )

    return {"id": str(pipeline.id), "code": pipeline.code, "name": pipeline.name}


@router.get("/pipelines", response_model=list[PipelineVersionOut])
async def list_pipelines(
    state: str | None = Query(default=None, description="Filter by version state"),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[PipelineVersionOut]:
    require_permission(context, "pipeline.read")

    # Outer join, not join: a pipeline with no versions yet still has to appear - it was
    # created before its first version can exist, and there is no other page a newly
    # created, still-versionless pipeline would show up on.
    query = (
        select(Pipeline, PipelineVersion)
        .outerjoin(PipelineVersion, PipelineVersion.pipeline_id == Pipeline.id)
        .order_by(Pipeline.code, PipelineVersion.version_number.desc())
    )
    if state:
        # A state filter naturally excludes versionless pipelines too - filtering by a
        # version's state has nothing to say about a pipeline with none, which is the
        # right behaviour: filtered views are about versions, the unfiltered one is
        # about pipelines.
        query = query.where(PipelineVersion.state == state)

    result = await db.execute(query)
    return [_to_out(pipeline, version) for pipeline, version in result.all()]


@router.post("/pipelines/{pipeline_id}/versions", response_model=PipelineVersionOut)
async def create_pipeline_version(
    pipeline_id: uuid.UUID,
    body: CreateVersionRequest,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> PipelineVersionOut:
    require_permission(context, "pipeline.manage")

    pipeline = (await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))).scalar_one_or_none()
    if pipeline is None:
        raise NotFoundError("Pipeline not found.")

    for stage in body.stages:
        if not await _model_has_deployable_version(db, stage.model_name):
            raise ApiError(
                status_code=422,
                code="pipeline_stage_model_not_deployable",
                message=(
                    f"Model '{stage.model_name}' has no version in a deployable state "
                    f"({', '.join(sorted(DEPLOYABLE_STATES))}). A pipeline cannot be "
                    "published against a model that isn't ready to run."
                ),
                details={"model_name": stage.model_name},
            )

    definition_json = {
        "stages": [stage.model_dump() for stage in body.stages],
    }
    digest = _definition_sha256(definition_json)

    existing_digest = await db.execute(
        select(PipelineVersion.id).where(PipelineVersion.definition_sha256 == digest)
    )
    if existing_digest.first() is not None:
        raise ConflictError("An identical pipeline definition is already registered as a version.")

    next_number = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM pipeline_versions "
                "WHERE pipeline_id = :pipeline_id"
            ),
            {"pipeline_id": pipeline_id},
        )
    ).scalar_one()

    version = PipelineVersion(
        pipeline_id=pipeline_id,
        version_number=next_number,
        schema_version=body.schema_version,
        definition_json=definition_json,
        definition_sha256=digest,
        allowed_overrides_schema=body.allowed_overrides_schema,
        runtime_target=body.runtime_target,
        resource_profile=body.resource_profile,
        created_by=context.developer_user_id,
    )
    db.add(version)
    await db.flush()

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="pipeline.version.create",
        outcome="success",
        target_type="pipeline_version",
        target_id=str(version.id),
        after_patch={"pipeline_id": str(pipeline_id), "version_number": next_number, "state": "draft"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )

    return _to_out(pipeline, version)


async def _transition_version(
    version_id: uuid.UUID,
    target_state: str,
    body: TransitionRequest,
    context: PlatformContext,
    db: AsyncSession,
) -> PipelineVersionOut:
    row = (
        await db.execute(
            select(Pipeline, PipelineVersion)
            .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
            .where(PipelineVersion.id == version_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Pipeline version not found.")
    pipeline, version = row

    allowed = VERSION_TRANSITIONS.get(version.state, set())
    if target_state not in allowed:
        raise ConflictError(
            f"Cannot move a pipeline version from '{version.state}' to '{target_state}'. "
            f"Allowed from here: {', '.join(sorted(allowed)) or 'none (terminal state)'}."
        )

    previous_state = version.state
    params: dict = {"id": version_id, "state": target_state}
    set_clause = "state = :state"
    if target_state == "published":
        set_clause += ", approved_by = :approved_by, approved_at = now()"
        params["approved_by"] = context.developer_user_id
    # Only the state/approval columns are writable; the immutability trigger (migration
    # 0031) rejects any attempt to alter the definition itself.
    await db.execute(text(f"UPDATE pipeline_versions SET {set_clause} WHERE id = :id"), params)

    event_type = f"pipeline.version.{target_state}.v1"
    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action=f"pipeline.version.{target_state}",
        outcome="success",
        target_type="pipeline_version",
        target_id=str(version_id),
        reason=body.reason,
        before_patch={"state": previous_state},
        after_patch={"state": target_state},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type=event_type,
        event_payload={
            "pipeline_version_id": str(version_id),
            "pipeline_code": pipeline.code,
            "previous_state": previous_state,
            "new_state": target_state,
        },
        aggregate_type="pipeline_version",
        aggregate_id=str(version_id),
    )

    # Built from the transition just applied, not re-read from `version` - that ORM
    # object still holds its pre-update snapshot (the state change above went through
    # `text()`, deliberately, so the immutability trigger sees a plain UPDATE rather than
    # an ORM flush that might touch other columns), and mutating it here would leave the
    # session holding a dirty object racing the raw SQL that already committed the change.
    out = _to_out(pipeline, version)
    out.state = target_state
    if target_state == "published":
        out.approved_at = dt.datetime.now(dt.UTC)
    return out


@router.post("/pipeline-versions/{version_id}/publish", response_model=PipelineVersionOut)
async def publish_pipeline_version(
    version_id: uuid.UUID,
    body: TransitionRequest,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> PipelineVersionOut:
    require_permission(context, "pipeline.publish")
    return await _transition_version(version_id, "published", body, context, db)


@router.post("/pipeline-versions/{version_id}/deprecate", response_model=PipelineVersionOut)
async def deprecate_pipeline_version(
    version_id: uuid.UUID,
    body: TransitionRequest,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> PipelineVersionOut:
    require_permission(context, "pipeline.publish")
    return await _transition_version(version_id, "deprecated", body, context, db)
