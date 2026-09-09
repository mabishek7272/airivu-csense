"""Reads deployable model versions from the registry.

The runtime is a *consumer* of the registry, never an author: it can read model metadata
and it can record validation runs, but it cannot change a version's state. Promotion is a
governed action that belongs to the Admin API, where it is permission-checked and audited.

Only versions in a deployable state are ever returned. A revoked or deprecated artifact
must not become loadable just because it happens to sit in the cache - that is the whole
point of the state machine (TRD §15).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Mirrors DEPLOYABLE_STATES in admin_api/app/api/models.py.
DEPLOYABLE_STATES = ("validated", "staging", "production")

# States a pre-promotion validation run is allowed to load. Excludes only `revoked` and
# `deprecated` - the two states registry.py's own module docstring says must never become
# loadable again. `uploaded`/`validating` are deliberately included even though they are
# NOT in DEPLOYABLE_STATES: TRD gate 2-4 (load/shape compatibility, golden dataset
# functional tests) exists specifically to produce real evidence *before* a version is
# promoted, so a path that only worked on already-deployable versions could never validate
# a genuinely new model - it could only re-validate one already in production. This is
# used solely by get_by_version_id below (the validation-harness lookup, addressed by an
# exact version_id an operator/script already obtained from the Admin API) - the by-name
# lookups above stay deployable-only, unchanged, so ordinary inference traffic never gains
# access to an unvalidated artifact.
VALIDATABLE_STATES = ("uploaded", "validating", "validated", "staging", "production")


@dataclass(frozen=True)
class RegisteredModel:
    version_id: uuid.UUID
    model_name: str
    task_code: str
    version_label: str
    state: str
    access_classification: str
    framework: str
    runtime: str
    artifact_sha256: str
    bucket: str
    object_key: str
    size_bytes: int
    label_map: dict | None
    license_name: str | None

    @property
    def extension(self) -> str:
        # Object keys are content-addressed and end in the artifact's real extension.
        _, _, tail = self.object_key.rpartition(".")
        return f".{tail}" if tail else ""


_SELECT = """
    SELECT mv.id, m.name, m.task_code, mv.version_label, mv.state,
           mv.access_classification, mv.framework, mv.runtime, mv.artifact_sha256,
           so.bucket, so.object_key, so.size_bytes, mv.label_map,
           mv.license_metadata->>'license' AS license_name
    FROM model_versions mv
    JOIN models m ON m.id = mv.model_id
    JOIN stored_objects so ON so.id = mv.artifact_object_id
"""


def _row_to_model(row) -> RegisteredModel:
    return RegisteredModel(
        version_id=row[0],
        model_name=row[1],
        task_code=row[2],
        version_label=row[3],
        state=row[4],
        access_classification=row[5],
        framework=row[6],
        runtime=row[7],
        artifact_sha256=row[8],
        bucket=row[9],
        object_key=row[10],
        size_bytes=row[11],
        label_map=row[12],
        license_name=row[13],
    )


async def list_deployable(session: AsyncSession) -> list[RegisteredModel]:
    result = await session.execute(
        text(f"{_SELECT} WHERE mv.state = ANY(:states) ORDER BY m.name"),
        {"states": list(DEPLOYABLE_STATES)},
    )
    return [_row_to_model(row) for row in result.all()]


async def get_deployable_by_name(session: AsyncSession, model_name: str) -> RegisteredModel | None:
    """Newest deployable version of a named model.

    Returns None for a model that exists but is not deployable, so the caller reports
    "not available for inference" rather than a bare 404 that hides the real reason.
    """
    result = await session.execute(
        text(
            f"{_SELECT} WHERE m.name = :name AND mv.state = ANY(:states) "
            "ORDER BY mv.created_at DESC LIMIT 1"
        ),
        {"name": model_name, "states": list(DEPLOYABLE_STATES)},
    )
    row = result.first()
    return _row_to_model(row) if row else None


async def get_by_version_id(session: AsyncSession, version_id: uuid.UUID) -> RegisteredModel | None:
    """Exact version, for the validation harness - not filtered to DEPLOYABLE_STATES (see
    VALIDATABLE_STATES above), but still refuses a revoked/deprecated artifact."""
    result = await session.execute(
        text(f"{_SELECT} WHERE mv.id = :version_id AND mv.state = ANY(:states)"),
        {"version_id": version_id, "states": list(VALIDATABLE_STATES)},
    )
    row = result.first()
    return _row_to_model(row) if row else None


async def get_state_by_name(session: AsyncSession, model_name: str) -> str | None:
    """Current state of a model regardless of deployability, for accurate error messages."""
    result = await session.execute(
        text(
            "SELECT mv.state FROM model_versions mv JOIN models m ON m.id = mv.model_id "
            "WHERE m.name = :name ORDER BY mv.created_at DESC LIMIT 1"
        ),
        {"name": model_name},
    )
    row = result.first()
    return row[0] if row else None
