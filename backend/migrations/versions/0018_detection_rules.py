"""Detection rules: the stored form of what turns a detection into an incident.

`Rule` in the pipeline layer has always carried the docstring "as it will be stored in a
published pipeline version". That pipeline-version schema is still ahead of us (Phase 4),
and in the meantime every rule in the system was constructed in Python by a script - which
meant the detection pipeline had no production caller and no tenant could configure
anything at all.

This is the smaller, honest thing: rules that a tenant owns, stored per camera or per
site, with the thresholds the evaluator already understands. When pipeline versions land,
these become the payload a version publishes rather than being thrown away.

Design points:

  **Scope is camera-or-site, not camera-only.** "Nobody in the yard after 22:00" is a site
  rule; making a tenant re-enter it for each of forty cameras guarantees it will be
  inconsistent within a month. A NULL camera_id means every camera at the site.

  **Zone is optional and nullable.** A rule with no zone watches the whole frame. Deleting
  a zone sets it back to whole-frame rather than deleting the rule, because silently
  removing an intrusion rule is the worst possible outcome of tidying up a map.

  **Thresholds are constrained in the database.** A confidence of 5.0 or a negative
  cooldown cannot be stored, whatever a future API or a manual UPDATE tries. The evaluator
  clamps too, but a value that is nonsense should never reach it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "detection_rules",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=False),
        # NULL means every camera at the site.
        sa.Column("camera_id", postgresql.UUID(as_uuid=True), nullable=True),
        # NULL means the whole frame.
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type_code", sa.Text(), nullable=False),
        # The model classes that can trigger this rule, e.g. ["person"].
        sa.Column("alertable_classes", postgresql.JSONB(), nullable=False),
        sa.Column("min_confidence", sa.Numeric(4, 3), nullable=False, server_default="0.5"),
        sa.Column("severity", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("min_roi_overlap", sa.Numeric(4, 3), nullable=False, server_default="0.3"),
        sa.Column("min_consecutive_frames", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="300"),
        # Hour-of-day window in the site's timezone. Both NULL means always active.
        sa.Column("active_from_hour", sa.Integer(), nullable=True),
        sa.Column("active_to_hour", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        # A deleted zone widens the rule to the whole frame rather than deleting it.
        # Silently dropping an intrusion rule while tidying a site map is far worse than
        # a rule that briefly watches more than it used to.
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "min_confidence > 0 AND min_confidence <= 1", name="ck_rule_confidence"
        ),
        sa.CheckConstraint(
            "min_roi_overlap >= 0 AND min_roi_overlap <= 1", name="ck_rule_roi_overlap"
        ),
        sa.CheckConstraint(
            "min_consecutive_frames >= 1 AND min_consecutive_frames <= 100",
            name="ck_rule_consecutive_frames",
        ),
        sa.CheckConstraint(
            "cooldown_seconds >= 0 AND cooldown_seconds <= 86400", name="ck_rule_cooldown"
        ),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_rule_severity"
        ),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_rule_status"),
        sa.CheckConstraint(
            "jsonb_typeof(alertable_classes) = 'array' "
            "AND jsonb_array_length(alertable_classes) > 0",
            name="ck_rule_classes_non_empty",
        ),
        # Both set or both null. One half of an active-hours window is meaningless, and
        # the evaluator would have to guess what the other half meant.
        sa.CheckConstraint(
            "(active_from_hour IS NULL) = (active_to_hour IS NULL)",
            name="ck_rule_hours_paired",
        ),
        sa.CheckConstraint(
            "(active_from_hour IS NULL OR (active_from_hour BETWEEN 0 AND 23)) "
            "AND (active_to_hour IS NULL OR (active_to_hour BETWEEN 0 AND 23))",
            name="ck_rule_hours_range",
        ),
    )

    # The ingestion hot path: every incoming detection looks up the active rules for one
    # camera. Site-wide rules (camera_id IS NULL) have to come back from the same lookup,
    # which is why the index leads on site_id.
    op.create_index(
        "ix_detection_rules_lookup",
        "detection_rules",
        ["tenant_id", "site_id", "camera_id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.execute("ALTER TABLE detection_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE detection_rules FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON detection_rules
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON detection_rules TO csense_api")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON detection_rules TO csense_platform_api"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON detection_rules")
    op.drop_index("ix_detection_rules_lookup", table_name="detection_rules")
    op.drop_table("detection_rules")
