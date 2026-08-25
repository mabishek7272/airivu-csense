"""Phase 1 foundation: identity, organizations, tenants, roles/permissions/memberships,
audit_events, outbox_events, processed_events. Row-level security is enabled and forced
on tenant-owned tables (memberships, membership_resource_scopes) as the Phase 1 exit-gate
isolation proof (docs/06_IMPLEMENTATION_PLAN.md IMP-G1, docs/05_BACKEND_SCHEMA.md §15).

Revision ID: 0001
Revises:
Create Date: 2026-08-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB, UUID as PG_UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_ENUMS = [
    ("user_status", ["invited", "active", "locked", "disabled", "deleted"]),
    ("authenticator_type", ["webauthn", "totp", "recovery_code", "oidc"]),
    ("organization_type", ["platform", "direct_customer", "reseller", "reseller_customer"]),
    ("organization_status", ["provisioning", "active", "grace", "suspended", "closed"]),
    ("tenant_status", ["provisioning", "active", "restricted", "suspended", "closed"]),
    ("org_relationship_status", ["active", "suspended", "ended"]),
    ("membership_status", ["invited", "active", "suspended", "revoked"]),
    ("site_scope_mode", ["all", "selected", "none"]),
    ("role_type", ["system", "custom"]),
    ("role_audience", ["customer", "platform"]),
    ("permission_effect", ["allow", "deny"]),
    ("audit_outcome", ["success", "denied", "failed"]),
]


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    enum_types: dict[str, ENUM] = {}
    for name, values in _ENUMS:
        enum_type = ENUM(*values, name=name, create_type=True)
        enum_type.create(bind, checkfirst=True)
        enum_types[name] = enum_type

    def col_enum(name: str) -> ENUM:
        return ENUM(*dict(_ENUMS)[name], name=name, create_type=False)

    op.create_table(
        "users",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email_normalized", CITEXT(), nullable=False, unique=True),
        sa.Column("email_display", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("status", col_enum("user_status"), nullable=False, server_default="invited"),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, server_default="en-US"),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("security_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_users_status_updated_at", "users", ["status", "updated_at"])

    op.create_table(
        "user_authenticators",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", col_enum("authenticator_type"), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("public_key", sa.LargeBinary(), nullable=True),
        sa.Column("counter", sa.BigInteger(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "type", "credential_id", name="uq_user_authenticator"),
    )

    op.create_table(
        "organizations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_type", col_enum("organization_type"), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("slug", CITEXT(), nullable=False, unique=True),
        sa.Column("status", col_enum("organization_status"), nullable=False, server_default="provisioning"),
        sa.Column("billing_reference", sa.Text(), nullable=True),
        sa.Column("default_region", sa.Text(), nullable=False, server_default="local"),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "tenants",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("status", col_enum("tenant_status"), nullable=False, server_default="provisioning"),
        sa.Column("data_region", sa.Text(), nullable=False, server_default="local"),
        sa.Column("default_timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("retention_policy_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("security_policy_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "organization_relationships",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("parent_organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("child_organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False, server_default="reseller_customer"),
        sa.Column("status", col_enum("org_relationship_status"), nullable=False, server_default="active"),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("allocation_policy_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_org_relationship_active",
        "organization_relationships",
        ["parent_organization_id", "child_organization_id", "relationship_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "roles",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role_type", col_enum("role_type"), nullable=False, server_default="system"),
        sa.Column("audience", col_enum("role_audience"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "permissions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("description", sa.Text(), nullable=True),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", PG_UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", PG_UUID(as_uuid=True), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("effect", col_enum("permission_effect"), nullable=False, server_default="allow"),
    )

    op.create_table(
        "memberships",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", PG_UUID(as_uuid=True), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("status", col_enum("membership_status"), nullable=False, server_default="invited"),
        sa.Column("site_scope_mode", col_enum("site_scope_mode"), nullable=False, server_default="none"),
        sa.Column("invited_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("invited_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_membership_active_tenant_user",
        "memberships",
        ["tenant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_memberships_tenant_user", "memberships", ["tenant_id", "user_id"])

    op.create_table(
        "membership_resource_scopes",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("membership_id", PG_UUID(as_uuid=True), sa.ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("effect", col_enum("permission_effect"), nullable=False, server_default="allow"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_membership_scopes_tenant_membership", "membership_resource_scopes", ["tenant_id", "membership_id"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("represented_actor_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("support_grant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("outcome", col_enum("audit_outcome"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_patch", JSONB(), nullable=True),
        sa.Column("after_patch", JSONB(), nullable=True),
        sa.Column("ip_hash_or_encrypted", sa.Text(), nullable=True),
        sa.Column("device_context", JSONB(), nullable=True),
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("previous_hash", sa.Text(), nullable=True),
        sa.Column("event_hash", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_events_tenant_occurred", "audit_events", ["tenant_id", "occurred_at"])
    op.create_index("ix_audit_events_action_occurred", "audit_events", ["action", "occurred_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_redacted", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_outbox_unpublished", "outbox_events", ["next_attempt_at"], postgresql_where=sa.text("published_at IS NULL")
    )

    op.create_table(
        "processed_events",
        sa.Column("consumer_name", sa.Text(), primary_key=True),
        sa.Column("event_id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("result_hash", sa.Text(), nullable=True),
    )

    # --- Row-level security proof of concept (IMP-G1) ---
    # Two session GUCs drive every tenant-owned policy in this codebase:
    #   app.tenant_id   — set by tenant_session() for exactly one request's transaction
    #   app.is_platform — set by platform_session() for Admin API privileged repositories
    for table in ("memberships", "membership_resource_scopes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR coalesce(current_setting('app.is_platform', true)::boolean, false)
            )
            WITH CHECK (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR coalesce(current_setting('app.is_platform', true)::boolean, false)
            )
            """
        )

    # audit_events: tenant-scoped rows are readable by their tenant OR platform; rows with
    # a NULL tenant_id (platform-global events) are only readable by platform. Application
    # roles never get UPDATE/DELETE on this table (append-only, SCH §11.1).
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_events_tenant_isolation ON audit_events
        USING (
            (tenant_id IS NOT NULL AND tenant_id = current_setting('app.tenant_id', true)::uuid)
            OR coalesce(current_setting('app.is_platform', true)::boolean, false)
        )
        WITH CHECK (
            (tenant_id IS NOT NULL AND tenant_id = current_setting('app.tenant_id', true)::uuid)
            OR coalesce(current_setting('app.is_platform', true)::boolean, false)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_events_tenant_isolation ON audit_events")
    op.execute("DROP POLICY IF EXISTS membership_resource_scopes_tenant_isolation ON membership_resource_scopes")
    op.execute("DROP POLICY IF EXISTS memberships_tenant_isolation ON memberships")

    op.drop_table("processed_events")
    op.drop_table("outbox_events")
    op.drop_table("audit_events")
    op.drop_table("membership_resource_scopes")
    op.drop_table("memberships")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("organization_relationships")
    op.drop_table("tenants")
    op.drop_table("organizations")
    op.drop_table("user_authenticators")
    op.drop_table("users")

    for name, _values in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")
