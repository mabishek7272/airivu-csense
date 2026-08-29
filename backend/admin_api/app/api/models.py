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
from csense_shared.db.models import Model, ModelValidationRun, ModelVersion, StoredObject
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
    # The most recent validation run for this version, if any - read-only summary, not
    # editable here. None means no run has ever been recorded, which is itself meaningful
    # (distinct from a run that failed).
    latest_validation_status: str | None = None
    latest_validation_metrics: dict | None = None
    latest_validation_report_key: str | None = None


class ValidationRunIn(BaseModel):
    """Recorded by `scripts/run_model_validation.py` after it actually runs a golden
    dataset through the real model and uploads the full report to MinIO directly (the
    same `csense-models` bucket model artifacts already live in, via
    `upload_model_artifact`'s own established pattern) - this endpoint never runs
    anything itself and never touches inference; it only records evidence that already
    exists, and creates the `stored_objects` row pointing at it (the report's own facts -
    key/digest/size - travel here as plain values, matching how `upload_model_artifact`
    itself returns them rather than a DB row, since only the caller ever holds the
    permission context to decide the object's policy fields)."""

    suite_version: str = Field(min_length=1, max_length=64)
    environment: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern="^(passed|failed)$")
    metrics: dict = Field(default_factory=dict)
    thresholds: dict = Field(default_factory=dict)
    result_object_key: str | None = Field(default=None, max_length=500)
    result_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    result_size_bytes: int | None = Field(default=None, ge=0)
    runner_version: str | None = Field(default=None, max_length=64)
    failure_summary: str | None = Field(default=None, max_length=2000)


class ValidationRunOut(BaseModel):
    id: str
    model_version_id: str
    suite_version: str
    environment: str
    status: str
    metrics: dict | None
    thresholds: dict | None
    result_object_key: str | None
    created_at: str


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
    rows = result.all()
    latest_runs = await _latest_validation_runs(db, [version.id for _, version, _ in rows])

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
            **_validation_summary_fields(latest_runs.get(version.id)),
        )
        for model, version, obj in rows
    ]


async def _latest_validation_runs(db: AsyncSession, version_ids: list[uuid.UUID]) -> dict:
    """The most recent run per version, in one query rather than N - `DISTINCT ON`
    (ordered by version then recency) is Postgres's own idiom for exactly this "latest
    row per group" shape. A plain dict of plain values, not an ORM hydration, since this
    is read-only summary data assembled from a join, not a row this code will ever write
    back."""
    if not version_ids:
        return {}
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (r.model_version_id)
                   r.model_version_id, r.status, r.metrics, so.object_key
            FROM model_validation_runs r
            LEFT JOIN stored_objects so ON so.id = r.result_object_id
            WHERE r.model_version_id = ANY(:ids)
            ORDER BY r.model_version_id, r.created_at DESC
            """
        ),
        {"ids": version_ids},
    )
    return {row.model_version_id: row for row in result.all()}


def _validation_summary_fields(run) -> dict:
    if run is None:
        return {
            "latest_validation_status": None,
            "latest_validation_metrics": None,
            "latest_validation_report_key": None,
        }
    return {
        "latest_validation_status": run.status,
        "latest_validation_metrics": run.metrics,
        "latest_validation_report_key": run.object_key,
    }


@router.post(
    "/model-versions/{version_id}/validation-runs",
    response_model=ValidationRunOut,
    status_code=201,
)
async def record_validation_run(
    version_id: uuid.UUID,
    body: ValidationRunIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ValidationRunOut:
    """Records evidence a validation run already produced - never runs anything itself
    (see `scripts/run_model_validation.py`, the only real caller)."""
    require_permission(context, "model.promote")

    version = (
        await db.execute(select(ModelVersion).where(ModelVersion.id == version_id))
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("Model version not found.")

    result_object_id = None
    if body.result_object_key:
        if body.result_sha256 is None or body.result_size_bytes is None:
            raise ApiError(
                status_code=422,
                code="incomplete_result_object",
                message="result_sha256 and result_size_bytes are required alongside result_object_key.",
            )
        stored = StoredObject(
            bucket="csense-models",
            object_key=body.result_object_key,
            object_type="model_validation_report",
            mime_type="application/json",
            size_bytes=body.result_size_bytes,
            sha256=body.result_sha256,
            retention_class="default",
        )
        db.add(stored)
        await db.flush()
        result_object_id = stored.id

    run = ModelValidationRun(
        model_version_id=version_id,
        suite_version=body.suite_version,
        environment=body.environment,
        status=body.status,
        started_at=None,
        finished_at=None,
        metrics=body.metrics,
        thresholds=body.thresholds,
        result_object_id=result_object_id,
        failure_summary=body.failure_summary,
        runner_version=body.runner_version,
    )
    db.add(run)
    await db.flush()

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="model.validation_run.record",
        outcome="success",
        target_type="model_version",
        target_id=str(version_id),
        reason=f"{body.suite_version} on {body.environment}: {body.status}",
        before_patch=None,
        after_patch={"status": body.status, "metrics": body.metrics},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="model.validation_run.recorded.v1",
        event_payload={
            "model_version_id": str(version_id),
            "suite_version": body.suite_version,
            "status": body.status,
            "metrics": body.metrics,
        },
        aggregate_type="model_version",
        aggregate_id=str(version_id),
    )

    return ValidationRunOut(
        id=str(run.id),
        model_version_id=str(version_id),
        suite_version=run.suite_version,
        environment=run.environment,
        status=run.status,
        metrics=run.metrics,
        thresholds=run.thresholds,
        result_object_key=body.result_object_key,
        created_at=run.created_at.isoformat(),
    )


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

    if version.state == "validating" and body.target_state == "validated":
        # The one transition this whole feature exists to gate: `validated` should mean
        # a golden-dataset run actually passed, not just that someone clicked promote.
        # Every other transition (including plain uploaded -> validating, which
        # scripts/e2e_model_registry.py already exercises through this same endpoint) is
        # untouched.
        latest = (
            await db.execute(
                select(ModelValidationRun)
                .where(ModelValidationRun.model_version_id == version_id)
                .order_by(ModelValidationRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None or latest.status != "passed":
            raise ApiError(
                status_code=422,
                code="validation_run_required",
                message=(
                    "Promoting to 'validated' needs a passing validation run recorded "
                    "first (POST .../validation-runs) - "
                    + ("none has been recorded for this version." if latest is None
                       else f"the most recent one is '{latest.status}', not 'passed'.")
                ),
                details={"model": model.name, "version_id": str(version_id)},
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
