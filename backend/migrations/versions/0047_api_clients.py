"""Scoped API keys for tenant integrations (SCH §10.5 `api_clients`/`api_keys`,
previously spec'd but never built). CHECKLIST: "Scoped API keys, rate limits, usage
metering, developer API docs" - this is the schema + credential-lookup half; rate
limiting and usage metering live in `csense_shared.security.rate_limit` (Redis-backed,
no schema of their own), and developer API docs are FastAPI's own `/docs`/`/openapi.json`
(already live, nothing to build).

**One deviation from SCH's literal field list, named here rather than silently made**:
SCH lists `rate_policy_id` on `api_clients`, implying a shared, reusable rate-policy
table. No such table exists anywhere else in this schema (`webhook_endpoints` has the
same `rate_policy` field in SCH §10.6 and was itself built without it - migration 0045).
Rather than invent a policy table nothing else uses yet, this ships a direct
`rate_limit_per_minute` column, exactly the simplification `webhook_endpoints` already
made. A real shared rate-policy table is a reasonable future refactor once more than one
caller needs the same limit reused, not a blocker for this pass.

**Tenant-owned only this pass.** SCH allows `tenant_id null` on `api_clients`
(presumably for a future platform-level client), but nothing in this codebase issues
platform-scoped API keys yet, and a nullable tenant_id would need its own RLS carve-out
for no real user today. `tenant_id` is NOT NULL here; widening it is a small, additive
migration whenever a platform-level API client is actually needed.

`api_keys.tenant_id` is a deliberate denormalization from `api_clients.tenant_id` (the
same choice `edge_devices` already makes for its own credential) - the credential lookup
that resolves a key does not yet know which tenant it belongs to (that is what the lookup
*discovers*), so the lookup function reads it straight off the key row rather than
joining through RLS-protected `api_clients` first.

`api_key_lookup` mirrors `edge_agent_lookup` (migration 0026) exactly: a
`SECURITY DEFINER` function taking a prefix and returning one row, because resolving a
credential is what establishes tenant scope and therefore cannot itself be scoped by RLS.

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

API_CLIENT_STATUS_VALUES = ["active", "disabled", "revoked"]
SITE_SCOPE_MODE_VALUES = ["all", "none"]  # "selected" deferred, same reasoning as memberships (0035)

TENANT_OWNED_TABLES = ("api_clients", "api_keys")


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

    ENUM(*API_CLIENT_STATUS_VALUES, name="api_client_status", create_type=True).create(bind, checkfirst=True)
    status_col = ENUM(*API_CLIENT_STATUS_VALUES, name="api_client_status", create_type=False)

    op.create_table(
        "api_clients",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Free text describing the client's purpose ("integration", "automation", ...) -
        # not branched on anywhere yet, kept for the same reason edge_devices.role is
        # free text: a real taxonomy can be added later without a migration to introduce
        # the column itself.
        sa.Column("audience", sa.Text(), nullable=False, server_default="integration"),
        sa.Column("status", status_col, nullable=False, server_default="active"),
        # A subset of the creating user's own permissions at creation time (enforced in
        # the endpoint, not the database - the same "checked at the boundary, not
        # reinvented in SQL" choice this codebase already makes for role_permissions
        # deny-wins). A key can only ever be as powerful as the person who issued it.
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("site_scope_mode", sa.Text(), nullable=False, server_default="all"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "site_scope_mode IN (" + ", ".join(f"'{v}'" for v in SITE_SCOPE_MODE_VALUES) + ")",
            name="ck_api_clients_site_scope_mode",
        ),
        sa.CheckConstraint("rate_limit_per_minute > 0", name="ck_api_clients_rate_limit_positive"),
    )
    op.create_index("ix_api_clients_tenant", "api_clients", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("api_client_id", PG_UUID(as_uuid=True), sa.ForeignKey("api_clients.id", ondelete="CASCADE"), nullable=False),
        # Denormalized from api_clients.tenant_id - see module docstring.
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rotation_parent_id", PG_UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_api_keys_client", "api_keys", ["api_client_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["key_prefix"], unique=True)

    for table in TENANT_OWNED_TABLES:
        _enable_rls(table)

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in TENANT_OWNED_TABLES:
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')

    # --- API key authentication lookup --------------------------------------------------
    #
    # Same shape, and the same reasoning, as edge_agent_lookup in 0026: a caller
    # presenting an API key does not send a tenant, and the credential is what discovers
    # it. Returns everything current_api_client() needs (scopes, site scope, rate limit)
    # in one round trip rather than a lookup followed by a second RLS-scoped query that
    # cannot run yet because the tenant scope has not been set.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION api_key_lookup(p_prefix text)
        RETURNS TABLE (
            key_id uuid,
            api_client_id uuid,
            tenant_id uuid,
            secret_hash text,
            key_revoked_at timestamptz,
            key_expires_at timestamptz,
            client_name text,
            client_status text,
            client_scopes text[],
            client_site_scope_mode text,
            client_rate_limit_per_minute integer,
            client_expires_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT
                k.id, k.api_client_id, k.tenant_id, k.secret_hash,
                k.revoked_at, k.expires_at,
                c.name, c.status::text, c.scopes, c.site_scope_mode,
                c.rate_limit_per_minute, c.expires_at
            FROM public.api_keys k
            JOIN public.api_clients c ON c.id = k.api_client_id
            WHERE k.key_prefix = p_prefix
            LIMIT 1;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION api_key_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO csense_api")
    op.execute("GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO csense_platform_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS api_key_lookup(text)")
    op.drop_table("api_keys")
    op.drop_table("api_clients")
    op.execute("DROP TYPE IF EXISTS api_client_status")
