"""Pipeline schema, stage registry, versioning, tenant assignment.

Implements docs/05_BACKEND_SCHEMA.md §8.4, §8.5, §8.8 - the three load-bearing pipeline
tables. `pipeline_test_runs` (§8.6) and `pipeline_deployments` (§8.7) are deliberately not
created here: the former needs a golden-dataset/benchmark harness that does not exist yet
(same reasoning `model_validation_runs` already sits on - a table with no runner would be
worse than no table), and the latter is fleet-scale canary/rollout machinery with no real
fleet to canary across yet. `pipeline_assignments.deployment_id` stays nullable and always
NULL for now - the schema doc itself already treats that as a valid shape, not a stopgap.

Platform-global for `pipelines`/`pipeline_versions` (SCH §8.4/8.5 mark them as such - a
pipeline definition is not tenant-owned data, exactly the same reasoning migration 0006
gave `models`/`model_versions`): no RLS, just role grants. Tenant-owned for
`pipeline_assignments` (SCH §8.8: `tenant_id` is the first field listed): FORCE ROW LEVEL
SECURITY with the same tenant-match-or-platform-group policy migration 0009 already
established for `sites`/`cameras`/`incidents`.

Also adds the FK `detections.pipeline_version_id -> pipeline_versions.id` that migration
0012 deliberately left as a bare nullable UUID column, for exactly this migration to close.

**Immutability**: `pipeline_versions` has no update-draft endpoint at all - a version is
created once, fully formed, and only ever transitions `state` after that (draft ->
published -> deprecated). The trigger below mirrors migration 0006's
`model_versions_immutable` shape: reject any change to `definition_json` and its digest,
unconditionally, so no application bug or ad-hoc SQL can silently repoint a version at a
different definition after the fact - simpler than `model_versions`' trigger only because
there is no draft-vs-published distinction to carve an exception around; nothing here ever
updates those columns at all.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-29
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

PIPELINE_VERSION_STATE_VALUES = ["draft", "published", "deprecated"]
PIPELINE_ASSIGNMENT_STATUS_VALUES = ["active", "superseded", "revoked"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    version_state = ENUM(*PIPELINE_VERSION_STATE_VALUES, name="pipeline_version_state", create_type=True)
    version_state.create(bind, checkfirst=True)
    version_state_col = ENUM(*PIPELINE_VERSION_STATE_VALUES, name="pipeline_version_state", create_type=False)

    assignment_status = ENUM(
        *PIPELINE_ASSIGNMENT_STATUS_VALUES, name="pipeline_assignment_status", create_type=True
    )
    assignment_status.create(bind, checkfirst=True)
    assignment_status_col = ENUM(
        *PIPELINE_ASSIGNMENT_STATUS_VALUES, name="pipeline_assignment_status", create_type=False
    )

    # --- pipelines (SCH §8.4) — platform-global, template/family -----------------
    op.create_table(
        "pipelines",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("use_case", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_team", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("code", name="uq_pipelines_code"),
        sa.CheckConstraint("status IN ('active', 'deprecated')", name="ck_pipelines_status"),
    )

    # --- pipeline_versions (SCH §8.5) — immutable after creation ------------------
    op.create_table(
        "pipeline_versions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("pipeline_id", PG_UUID(as_uuid=True), sa.ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        # A stage list - only a leading {"type": "infer", ...} stage is interpreted
        # anywhere today (see backend/admin_api/app/api/pipelines.py). The shape leaves
        # room for preprocess/filter/tracking stages from the TRD's own architecture
        # diagram (§16) to be added later without another migration.
        sa.Column("definition_json", JSONB(), nullable=False),
        sa.Column("definition_sha256", sa.Text(), nullable=False),
        sa.Column("allowed_overrides_schema", JSONB(), nullable=False, server_default="{}"),
        sa.Column("runtime_target", sa.Text(), nullable=False, server_default="cloud"),
        sa.Column("resource_profile", JSONB(), nullable=True),
        sa.Column("state", version_state_col, nullable=False, server_default="draft"),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("approved_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("pipeline_id", "version_number", name="uq_pipeline_version_number"),
        sa.UniqueConstraint("definition_sha256", name="uq_pipeline_version_definition_sha256"),
        sa.CheckConstraint("runtime_target IN ('cloud', 'edge')", name="ck_pipeline_version_runtime_target"),
        sa.CheckConstraint("definition_sha256 ~ '^[0-9a-f]{64}$'", name="ck_pipeline_version_sha256_format"),
    )
    op.create_index("ix_pipeline_versions_pipeline_state", "pipeline_versions", ["pipeline_id", "state"])

    # --- pipeline_assignments (SCH §8.8) — tenant-owned ---------------------------
    op.create_table(
        "pipeline_assignments",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("camera_id", PG_UUID(as_uuid=True), sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pipeline_version_id", PG_UUID(as_uuid=True), sa.ForeignKey("pipeline_versions.id"), nullable=False),
        # Always NULL in this pass - see the module docstring. Not yet a real FK to
        # anything, since pipeline_deployments does not exist; kept as a plain nullable
        # column now so a later migration can add the FK without touching this table's
        # shape.
        sa.Column("deployment_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("runtime_location", sa.Text(), nullable=False, server_default="cloud"),
        sa.Column("tenant_overrides", JSONB(), nullable=False, server_default="{}"),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", assignment_status_col, nullable=False, server_default="active"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("runtime_location IN ('cloud', 'edge')", name="ck_pipeline_assignment_runtime_location"),
    )
    # SCH §8.8: "prevents overlapping active assignment for the same (camera_id,
    # pipeline/use_case, priority)". A partial unique index on (camera_id, priority)
    # while active is a simplified form of that - it stops two active assignments from
    # ever competing for the same priority slot on one camera, without a full
    # overlapping-*time-range* exclusion (that needs the btree_gist extension and buys
    # nothing yet, since effective_to is unused by anything that reads assignments today).
    op.create_index(
        "uq_pipeline_assignments_camera_priority_active",
        "pipeline_assignments", ["camera_id", "priority"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_pipeline_assignments_tenant_camera", "pipeline_assignments", ["tenant_id", "camera_id"])

    # --- Immutability trigger, mirrors migration 0006's model_versions_immutable ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION csense_pipeline_versions_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.pipeline_id IS DISTINCT FROM OLD.pipeline_id
               OR NEW.version_number IS DISTINCT FROM OLD.version_number
               OR NEW.definition_json IS DISTINCT FROM OLD.definition_json
               OR NEW.definition_sha256 IS DISTINCT FROM OLD.definition_sha256
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION
                    'pipeline_versions.% is immutable once created; create a new version instead',
                    'definition columns'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER pipeline_versions_immutable
        BEFORE UPDATE ON pipeline_versions
        FOR EACH ROW EXECUTE FUNCTION csense_pipeline_versions_immutable()
        """
    )

    # --- Close the gap migration 0012 deliberately left open ----------------------
    op.create_foreign_key(
        "fk_detections_pipeline_version", "detections", "pipeline_versions",
        ["pipeline_version_id"], ["id"],
    )

    # --- RLS on pipeline_assignments, same policy shape as migration 0009 --------
    op.execute("ALTER TABLE pipeline_assignments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pipeline_assignments FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY pipeline_assignments_tenant_isolation ON pipeline_assignments
        USING (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        WITH CHECK (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        """
    )

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in ("pipelines", "pipeline_versions", "pipeline_assignments"):
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')


def downgrade() -> None:
    op.drop_constraint("fk_detections_pipeline_version", "detections", type_="foreignkey")
    op.execute("DROP TRIGGER IF EXISTS pipeline_versions_immutable ON pipeline_versions")
    op.execute("DROP FUNCTION IF EXISTS csense_pipeline_versions_immutable()")
    op.drop_table("pipeline_assignments")
    op.drop_table("pipeline_versions")
    op.drop_table("pipelines")
    op.execute("DROP TYPE IF EXISTS pipeline_assignment_status")
    op.execute("DROP TYPE IF EXISTS pipeline_version_state")
