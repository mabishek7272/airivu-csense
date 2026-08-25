"""AI model registry: models, model_versions, validation runs, stored objects.

Implements docs/05_BACKEND_SCHEMA.md §8.1–8.3 and §11.5, and the immutability rules from
docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §15 ("Artifacts are content-addressed and
cannot be overwritten").

Platform-global by design (SCH §8.1/8.2 mark models and model_versions as such): a model
artifact is not tenant-owned data, so these tables carry no tenant_id and are unreachable
from tenant-scoped repositories by construction. Tenants reach models only indirectly,
through pipeline assignments, which are tenant-owned.

Immutability is enforced in the database rather than by convention:
  - `artifact_sha256` is UNIQUE, so the same bytes cannot be registered twice under
    different identities;
  - a trigger rejects UPDATEs to a published version's identity/artifact columns, allowing
    only the documented state transitions.

`access_classification` carries the biometric flag. The legacy platform's staff-attendance
feature uses InsightFace face-embedding models, which docs/01_PRODUCT_REQUIREMENTS_DOCUMENT.md
excludes from release one. Those versions are imported in the `revoked` state and cannot be
promoted without a deliberate, audited state change — see CLARIFICATIONS.md.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

MODEL_STATE_VALUES = [
    "uploaded",
    "validating",
    "validated",
    "staging",
    "production",
    "deprecated",
    "revoked",
]


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    model_state = ENUM(*MODEL_STATE_VALUES, name="model_version_state", create_type=True)
    model_state.create(bind, checkfirst=True)
    state_col = ENUM(*MODEL_STATE_VALUES, name="model_version_state", create_type=False)

    # --- stored_objects (SCH §11.5) ---------------------------------------------
    op.create_table(
        "stored_objects",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("encryption_key_ref", sa.Text(), nullable=True),
        sa.Column("retention_class", sa.Text(), nullable=False, server_default="default"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("bucket", "object_key", name="uq_stored_object_location"),
    )
    op.create_index("ix_stored_objects_sha256", "stored_objects", ["sha256"])
    op.create_index(
        "ix_stored_objects_tenant", "stored_objects", ["tenant_id"],
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )

    # --- models (SCH §8.1) — platform-global -------------------------------------
    op.create_table(
        "models",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("task_code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_team", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("default_label_schema", JSONB(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_models_name"),
    )
    op.create_index("ix_models_task_code", "models", ["task_code"])

    # --- model_versions (SCH §8.2) — immutable after publish ---------------------
    op.create_table(
        "model_versions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", PG_UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("artifact_object_id", PG_UUID(as_uuid=True), sa.ForeignKey("stored_objects.id"), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("framework", sa.Text(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("input_schema", JSONB(), nullable=True),
        sa.Column("output_schema", JSONB(), nullable=True),
        sa.Column("label_map", JSONB(), nullable=True),
        sa.Column("hardware_profile", JSONB(), nullable=True),
        sa.Column("license_metadata", JSONB(), nullable=True),
        sa.Column("provenance", JSONB(), nullable=True),
        # 'standard' | 'biometric' | 'regulated' — gates what may be promoted, and is
        # surfaced in the console so an operator cannot enable biometric inference by
        # accident (PRD release-one non-goal).
        sa.Column("access_classification", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("state", state_col, nullable=False, server_default="uploaded"),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("model_id", "version_label", name="uq_model_version_label"),
        # Content addressing: identical bytes can never be registered as two versions.
        sa.UniqueConstraint("artifact_sha256", name="uq_model_version_artifact_sha256"),
        sa.CheckConstraint(
            "access_classification IN ('standard', 'biometric', 'regulated')",
            name="ck_model_version_access_classification",
        ),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_model_version_sha256_format"),
    )
    op.create_index("ix_model_versions_model_state", "model_versions", ["model_id", "state"])
    op.create_index("ix_model_versions_classification", "model_versions", ["access_classification"])

    # --- model_validation_runs (SCH §8.3) ----------------------------------------
    op.create_table(
        "model_validation_runs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "model_version_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("suite_version", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metrics", JSONB(), nullable=True),
        sa.Column("thresholds", JSONB(), nullable=True),
        sa.Column("result_object_id", PG_UUID(as_uuid=True), sa.ForeignKey("stored_objects.id"), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("runner_version", sa.Text(), nullable=True),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_model_validation_runs_version", "model_validation_runs", ["model_version_id", "created_at"]
    )

    # --- Immutability trigger (TRD §15, SCH §19) ---------------------------------
    # "Model/pipeline published versions cannot be updated; only state transitions and
    # new versions are allowed." Enforced here so no application bug or ad-hoc SQL can
    # silently repoint a published version at different bytes.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION csense_model_versions_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.model_id IS DISTINCT FROM OLD.model_id
               OR NEW.version_label IS DISTINCT FROM OLD.version_label
               OR NEW.artifact_object_id IS DISTINCT FROM OLD.artifact_object_id
               OR NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256
               OR NEW.framework IS DISTINCT FROM OLD.framework
               OR NEW.runtime IS DISTINCT FROM OLD.runtime
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION
                    'model_versions.% is immutable once created; create a new version instead',
                    'identity/artifact columns'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER model_versions_immutable
        BEFORE UPDATE ON model_versions
        FOR EACH ROW EXECUTE FUNCTION csense_model_versions_immutable()
        """
    )

    # Grants for tables created after migration 0004 ran.
    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in ("stored_objects", "models", "model_versions", "model_validation_runs"):
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS model_versions_immutable ON model_versions")
    op.execute("DROP FUNCTION IF EXISTS csense_model_versions_immutable()")
    op.drop_table("model_validation_runs")
    op.drop_table("model_versions")
    op.drop_table("models")
    op.drop_table("stored_objects")
    op.execute("DROP TYPE IF EXISTS model_version_state")
