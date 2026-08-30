"""Signed commands with expiry/idempotency - desired-state push to the device (SCH §7.5
`device_commands`, previously spec'd but never built).

`edge_devices` gets the two version counters SCH §7.3 already specifies
(`desired_state_version`/`observed_state_version`) - the control plane's target vs. what
the device last confirmed applying. Neither this migration nor the endpoints built
against it drive them yet (a version bump belongs to whatever feature first has a real
desired state to converge on - a pipeline assignment, a config change); they exist now so
`device_commands` has somewhere to point once one does, matching the schema's own shape.

**Idempotency is a database constraint, not just an application check**: `(edge_device_id,
idempotency_key)` unique - a retried issuance genuinely cannot create a second row, not
merely "shouldn't" if the application code path is followed correctly.

Tenant-owned: FORCE ROW LEVEL SECURITY, same tenant-match-or-platform-group policy as
every table added this session.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

COMMAND_STATUS_VALUES = ["pending", "delivered", "completed", "failed", "expired"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("edge_devices", sa.Column("desired_state_version", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("edge_devices", sa.Column("observed_state_version", sa.BigInteger(), nullable=False, server_default="0"))

    ENUM(*COMMAND_STATUS_VALUES, name="device_command_status", create_type=True).create(bind, checkfirst=True)
    status_col = ENUM(*COMMAND_STATUS_VALUES, name="device_command_status", create_type=False)

    op.create_table(
        "device_commands",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("edge_device_id", PG_UUID(as_uuid=True), sa.ForeignKey("edge_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("command_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", status_col, nullable=False, server_default="pending"),
        sa.Column("not_before", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("issued_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result_code", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        # The exact JWT returned at issuance - stored, not regenerated, so a retried
        # delivery (or an idempotent re-issuance) hands back the identical signed
        # envelope rather than a fresh one with a different `iat`.
        sa.Column("signed_envelope", sa.Text(), nullable=False),
    )
    op.create_index(
        "uq_device_commands_idempotency", "device_commands", ["edge_device_id", "idempotency_key"], unique=True,
    )
    op.create_index("ix_device_commands_pending", "device_commands", ["edge_device_id", "status"])

    op.execute("ALTER TABLE device_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE device_commands FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY device_commands_tenant_isolation ON device_commands
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
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON device_commands TO "{role}"')


def downgrade() -> None:
    op.drop_table("device_commands")
    op.execute("DROP TYPE IF EXISTS device_command_status")
    op.drop_column("edge_devices", "observed_state_version")
    op.drop_column("edge_devices", "desired_state_version")
