"""Async report/export jobs (CHECKLIST: "Async reports/exports with time-limited
download"). Generation happens off the request path (FastAPI `BackgroundTasks`, not a
separate worker process this deployment doesn't have anywhere to run) - the request that
creates a job gets back a `queued` row immediately; the file itself lands in the existing
`csense-exports` MinIO bucket (already reserved in `storage/objects.py`, previously
unused) once generation finishes.

No new permission: exporting incidents you can already read is scoped by the same
`incident.read` the list/detail endpoints already require - a genuinely new resource type
worth its own permission only once export covers something a reader couldn't otherwise
see.

Expiry is lazy, the same precedent `sync_license_status` (licensing/lifecycle.py) and
support-grant reads already established: every read of a job flips a completed job past
its own `expires_at` to `expired` first, rather than a scheduled cleanup job this
deployment has nowhere to run.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-31
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

EXPORT_JOB_STATUS_VALUES = ["queued", "processing", "completed", "failed", "expired"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
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


def upgrade() -> None:
    bind = op.get_bind()

    ENUM(*EXPORT_JOB_STATUS_VALUES, name="export_job_status", create_type=True).create(bind, checkfirst=True)
    status_col = ENUM(*EXPORT_JOB_STATUS_VALUES, name="export_job_status", create_type=False)

    op.create_table(
        "export_jobs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("export_type", sa.Text(), nullable=False),  # e.g. "incidents" - the only kind this pass ships
        sa.Column("filters", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", status_col, nullable=False, server_default="queued"),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Only set once the job completes (requested_at is not itself a useful expiry
        # anchor - a job stuck in `processing` should not silently start expiring).
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_export_jobs_tenant_requested", "export_jobs", ["tenant_id", "requested_at"])

    _enable_rls("export_jobs")

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON export_jobs TO "{role}"')


def downgrade() -> None:
    op.drop_table("export_jobs")
    op.execute("DROP TYPE IF EXISTS export_job_status")
