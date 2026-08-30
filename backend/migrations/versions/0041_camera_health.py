"""Camera health telemetry history (CHECKLIST's own note: "MongoDB was removed from the
stack (CLARIFICATIONS #19/#20); this will land in PostgreSQL like detections did, not
Mongo as originally spec'd").

**Current-state already exists, it turns out** - migration 0020 already gave `cameras`
`last_probed_at`/`last_error`/`stream_profile`, populated by every `POST /cameras/{id}
/probe` call. That already answers "is this camera up right now"; duplicating it into a
second current-state table would just be two places for the same fact to disagree. What
was actually missing, and what CHECKLIST's "telemetry history" half names, is the
*history* behind that single current row - every probe overwrites the last one, so there
was no way to see a camera's health over time. `camera_health_events` is that append-only
log, written by the same `POST /cameras/{id}/probe` that already updates `cameras` -
making an already-real signal durable, not adding a new probing mechanism.

Tenant-owned: FORCE ROW LEVEL SECURITY with the current tenant-match-or-platform-group
policy (migration 0009/0031's, not migration 0001's older boolean-only one).

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

CAMERA_HEALTH_STATUS_VALUES = ["online", "offline"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    ENUM(*CAMERA_HEALTH_STATUS_VALUES, name="camera_health_status", create_type=True).create(bind, checkfirst=True)
    status_col = ENUM(*CAMERA_HEALTH_STATUS_VALUES, name="camera_health_status", create_type=False)

    op.create_table(
        "camera_health_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("camera_id", PG_UUID(as_uuid=True), sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", status_col, nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("codec", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("framerate", sa.Numeric(6, 2), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_camera_health_events_camera_time", "camera_health_events", ["camera_id", "occurred_at"])

    op.execute("ALTER TABLE camera_health_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE camera_health_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY camera_health_events_tenant_isolation ON camera_health_events
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
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON camera_health_events TO "{role}"')


def downgrade() -> None:
    op.drop_table("camera_health_events")
    op.execute("DROP TYPE IF EXISTS camera_health_status")
