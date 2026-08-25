"""Model registry endpoints (TRD §10.2: `/api/v1/admin/models`, `/model-versions/{id}/promote`).

Platform-only: model artifacts are not tenant-owned data, so these live behind the
platform token audience and a `model.read` / `model.promote` permission.

Promotion is where the governance sits. Biometric versions are refused a deployable
state unless the request carries an explicit acknowledgement, because facial recognition
is a release-one non-goal in the PRD and the deployment jurisdiction is unconfirmed. The
refusal is deliberate friction, not a technical limitation — an operator can still
promote, but must say so in a way that lands in the audit trail.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.db.models import Model, ModelVersion, StoredObject
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin", tags=["admin-models"])

# States from which a version can actually be picked up by a pipeline.
DEPLOYABLE_STATES = {"validated", "staging", "production"}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "uploaded": {"validating", "revoked"},
    "validating": {"validated", "uploaded", "revoked"},
    "validated": {"staging", "deprecated", "revoked"},
    "staging": {"production", "validated", "deprecated", "revoked"},
    "production": {"deprecated", "revoked"},
    "deprecated": {"staging", "revoked"},
    # Terminal by policy: a revoked artifact returns only through an explicit re-validation.
    "revoked": {"validating"},
}


class ModelVersionOut(BaseModel):
    id: str
    model_name: str
    task_code: str
    version_label: str
    state: str
    access_classification: str
    framework: str
    runtime: str
    artifact_sha256: str
    size_bytes: int
    license: str | None = None
    state_reason: str | None = None


class PromoteRequest(BaseModel):
    target_state: str = Field(description="One of: validating, validated, staging, production, deprecated, revoked")
    reason: str = Field(min_length=8, max_length=500, description="Recorded in the audit trail")
    acknowledge_biometric: bool = Field(
        default=False,
        description=(
            "Required to move a biometric model into a deployable state. Confirms the "
            "requester accepts that facial recognition is outside the approved release-one "
            "scope and that privacy/legal review has been completed."
        ),
    )


@router.get("/models", response_model=list[ModelVersionOut])
async def list_model_versions(
    state: str | None = Query(default=None, description="Filter by version state"),
    classification: str | None = Query(default=None, description="standard | biometric | regulated"),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[ModelVersionOut]:
    require_permission(context, "model.read")

    query = (
        select(Model, ModelVersion, StoredObject)
        .join(ModelVersion, ModelVersion.model_id == Model.id)
        .join(StoredObject, StoredObject.id == ModelVersion.artifact_object_id)
        .order_by(Model.name, ModelVersion.created_at.desc())
    )
    if state:
        query = query.where(ModelVersion.state == state)
    if classification:
        query = query.where(ModelVersion.access_classification == classification)

    result = await db.execute(query)
    return [
        ModelVersionOut(
            id=str(version.id),
            model_name=model.name,
            task_code=model.task_code,
            version_label=version.version_label,
            state=version.state,
            access_classification=version.access_classification,
            framework=version.framework,
            runtime=version.runtime,
            artifact_sha256=version.artifact_sha256,
            size_bytes=obj.size_bytes,
            license=(version.license_metadata or {}).get("license"),
            state_reason=version.state_reason,
        )
        for model, version, obj in result.all()
    ]


@router.post("/model-versions/{version_id}/promote", response_model=ModelVersionOut)
async def promote_model_version(
    version_id: uuid.UUID,
    body: PromoteRequest,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ModelVersionOut:
    require_permission(context, "model.promote")

    result = await db.execute(
        select(Model, ModelVersion, StoredObject)
        .join(ModelVersion, ModelVersion.model_id == Model.id)
        .join(StoredObject, StoredObject.id == ModelVersion.artifact_object_id)
        .where(ModelVersion.id == version_id)
    )
    row = result.first()
    if row is None:
        raise NotFoundError("Model version not found.")
    model, version, obj = row

    allowed = VALID_TRANSITIONS.get(version.state, set())
    if body.target_state not in allowed:
        raise ConflictError(
            f"Cannot move a version from '{version.state}' to '{body.target_state}'. "
            f"Allowed from here: {', '.join(sorted(allowed)) or 'none'}."
        )

    if (
        version.access_classification == "biometric"
        and body.target_state in DEPLOYABLE_STATES
        and not body.acknowledge_biometric
    ):
        raise ApiError(
            status_code=422,
            code="biometric_promotion_requires_acknowledgement",
            message=(
                "This is a biometric model. Facial recognition is outside the approved "
                "release-one scope, and biometric templates carry additional legal "
                "obligations. Re-submit with acknowledge_biometric=true to confirm "
                "privacy and legal review is complete."
            ),
            details={"model": model.name, "classification": version.access_classification},
        )

    previous_state = version.state
    # Only the state columns are writable; the immutability trigger (migration 0006)
    # rejects any attempt to alter identity or artifact columns.
    await db.execute(
        text(
            "UPDATE model_versions SET state = :state, state_reason = :reason WHERE id = :id"
        ),
        {"state": body.target_state, "reason": body.reason, "id": version_id},
    )

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="model.promote",
        outcome="success",
        target_type="model_version",
        target_id=str(version_id),
        reason=body.reason,
        before_patch={"state": previous_state},
        after_patch={"state": body.target_state},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="model.version.state.changed.v1",
        event_payload={
            "model_version_id": str(version_id),
            "model_name": model.name,
            "previous_state": previous_state,
            "new_state": body.target_state,
            "access_classification": version.access_classification,
            "biometric_acknowledged": body.acknowledge_biometric,
        },
        aggregate_type="model_version",
        aggregate_id=str(version_id),
    )

    return ModelVersionOut(
        id=str(version.id),
        model_name=model.name,
        task_code=model.task_code,
        version_label=version.version_label,
        state=body.target_state,
        access_classification=version.access_classification,
        framework=version.framework,
        runtime=version.runtime,
        artifact_sha256=version.artifact_sha256,
        size_bytes=obj.size_bytes,
        license=(version.license_metadata or {}).get("license"),
        state_reason=body.reason,
    )
