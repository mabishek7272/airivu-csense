"""Webhook endpoints + delivery history (SCH §10.6/§10.7, previously spec'd but never
built). "Webhook signing, verification, replay protection" - see `csense_shared.security
.webhooks` for the signing/verification half; this is the schema behind it.

Both `url` and `signing_secret` are envelope-encrypted through the same `encrypted_secrets`
table (and `write_secret`/`read_secret` functions) every other sensitive value in this
codebase already goes through - camera credentials, TOTP secrets - rather than a new,
parallel encrypted-column scheme. SCH's own field name (`url_encrypted`) already signals
the URL itself is sensitive: a webhook destination can embed a path-based token some
receivers use as their own auth (Slack's own incoming-webhook URLs are exactly this
shape), so it gets the same treatment a credential would, not just the signing secret.

`webhook_deliveries` is intentionally not append-only-enforced at the database level the
way `audit_events` is - a delivery attempt's own `status`/`response_status`/
`next_attempt_at` are expected to be updated as retries happen, unlike an audit record.

Both tenant-owned: FORCE ROW LEVEL SECURITY, the same tenant-match-or-platform-group
policy every tenant-owned table this session added already uses.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

WEBHOOK_ENDPOINT_STATUS_VALUES = ["active", "disabled"]
WEBHOOK_DELIVERY_STATUS_VALUES = ["pending", "succeeded", "failed", "abandoned"]

TENANT_OWNED_TABLES = ("webhook_endpoints", "webhook_deliveries")


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

    ENUM(*WEBHOOK_ENDPOINT_STATUS_VALUES, name="webhook_endpoint_status", create_type=True).create(bind, checkfirst=True)
    ENUM(*WEBHOOK_DELIVERY_STATUS_VALUES, name="webhook_delivery_status", create_type=True).create(bind, checkfirst=True)
    endpoint_status_col = ENUM(*WEBHOOK_ENDPOINT_STATUS_VALUES, name="webhook_endpoint_status", create_type=False)
    delivery_status_col = ENUM(*WEBHOOK_DELIVERY_STATUS_VALUES, name="webhook_delivery_status", create_type=False)

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url_secret_id", PG_UUID(as_uuid=True), sa.ForeignKey("encrypted_secrets.id"), nullable=False),
        sa.Column("url_host_display", sa.Text(), nullable=False),  # non-secret: just the hostname, shown in lists
        sa.Column("signing_secret_id", PG_UUID(as_uuid=True), sa.ForeignKey("encrypted_secrets.id"), nullable=False),
        sa.Column("event_filters", ARRAY(sa.Text()), nullable=False, server_default="{}"),  # empty = all events
        sa.Column("status", endpoint_status_col, nullable=False, server_default="active"),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_webhook_endpoints_tenant", "webhook_endpoints", ["tenant_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("webhook_endpoint_id", PG_UUID(as_uuid=True), sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", delivery_status_col, nullable=False, server_default="pending"),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failure_summary_redacted", sa.Text(), nullable=True),
    )
    op.create_index("ix_webhook_deliveries_endpoint_time", "webhook_deliveries", ["webhook_endpoint_id", "scheduled_at"])

    for table in TENANT_OWNED_TABLES:
        _enable_rls(table)

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in TENANT_OWNED_TABLES:
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
    op.execute("DROP TYPE IF EXISTS webhook_delivery_status")
    op.execute("DROP TYPE IF EXISTS webhook_endpoint_status")
