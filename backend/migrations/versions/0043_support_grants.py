"""Just-in-time support grants (SCH §5.10, previously spec'd but never built) - the
request/approve/revoke/expiry lifecycle and audit trail behind privileged, time-boxed
platform access into a tenant's own data.

`support_grant_id` already exists on `TenantContext`/`PlatformContext`
(`csense_shared/security/tenant_context.py`) and `record_audit_and_outbox` already
accepts one - both have carried the field since early in this build, with nothing to
populate it until now. This migration builds the real table those fields were always
meant to point at.

Two tables' worth of RLS shape, deliberately different: `support_grants.tenant_id` is
real tenant-owned data (a tenant must see grants requested against its own account, for
the "active support session" banner and its own revoke right) - tenant-match-or-platform-
group, the same policy every tenant-owned table this session added already uses.
`developer_user_id` stays a bare column with no RLS predicate on it, since platform
developers aren't tenant-scoped identities.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

SUPPORT_GRANT_STATUS_VALUES = ["requested", "approved", "denied", "active", "revoked", "expired"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    ENUM(*SUPPORT_GRANT_STATUS_VALUES, name="support_grant_status", create_type=True).create(bind, checkfirst=True)
    status_col = ENUM(*SUPPORT_GRANT_STATUS_VALUES, name="support_grant_status", create_type=False)

    op.create_table(
        "support_grants",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("developer_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_reference", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("requested_scopes", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("resource_scope", JSONB(), nullable=True),
        sa.Column("status", status_col, nullable=False, server_default="requested"),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approved_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_support_grants_developer_status", "support_grants", ["developer_user_id", "status", "expires_at"])
    op.create_index("ix_support_grants_tenant_status", "support_grants", ["tenant_id", "status", "expires_at"])

    op.execute("ALTER TABLE support_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE support_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY support_grants_tenant_isolation ON support_grants
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
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON support_grants TO "{role}"')


def downgrade() -> None:
    op.drop_table("support_grants")
    op.execute("DROP TYPE IF EXISTS support_grant_status")
