"""Licensing and quota schema (docs/05_BACKEND_SCHEMA.md §6.1-6.5) - the foundation the
"License plans, terms, entitlements, quota ledgers, concurrent reservation" CHECKLIST item
needs. No migration for this existed before now, unlike memberships/reseller (both already
had their schema from migration 0001).

`license_plans` is platform-global (a plan definition is not tenant-owned data - the same
reasoning migration 0006 gave `models`/`model_versions`, migration 0031 gave `pipelines`/
`pipeline_versions`): no RLS, just role grants. `licenses`, `license_entitlements`,
`quota_ledgers`, `quota_reservations` are tenant-owned: FORCE ROW LEVEL SECURITY with the
same tenant-match-or-platform-group policy migration 0009 established.

**`usage_buckets` (SCH §6.6) is deliberately not created this pass** - it is metering-
reconciliation storage for a metering pipeline that does not exist yet (API/event request
counters reconciled from Redis), a genuinely separate concern from the entitlement-gated
resource-creation quota this migration exists to support. Mirrors migration 0031's own
deferral of `pipeline_test_runs`/`pipeline_deployments` for the identical reason: a table
with no writer would be worse than no table.

**`licenses.status`** simplifies SCH §6.2's "one effective primary license per tenant and
product scope" to "one effective (active or grace) license per tenant" - there is exactly
one product scope in this system today, so the extra dimension has nothing to distinguish
yet; adding an unused column would be schema for a distinction that doesn't exist.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-30
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

LICENSE_STATUS_VALUES = ["scheduled", "active", "grace", "suspended", "expired", "revoked"]
QUOTA_RESERVATION_STATUS_VALUES = ["reserved", "committed", "released", "expired"]

TENANT_OWNED_TABLES = ("licenses", "license_entitlements", "quota_ledgers", "quota_reservations")
PLATFORM_GLOBAL_TABLES = ("license_plans",)


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def _enable_rls(table: str) -> None:
    """Same policy shape as migration 0009/0031: tenant match, or platform-group
    membership - the current best pattern, stronger than migration 0001's boolean-only
    `app.is_platform` check (`pg_has_role` means a compromised tenant connection setting
    that flag on its own session grants nothing)."""
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

    ENUM(*LICENSE_STATUS_VALUES, name="license_status", create_type=True).create(bind, checkfirst=True)
    ENUM(*QUOTA_RESERVATION_STATUS_VALUES, name="quota_reservation_status", create_type=True).create(
        bind, checkfirst=True
    )
    license_status_col = ENUM(*LICENSE_STATUS_VALUES, name="license_status", create_type=False)
    reservation_status_col = ENUM(*QUOTA_RESERVATION_STATUS_VALUES, name="quota_reservation_status", create_type=False)

    # --- license_plans: platform-global (SCH §6.1) -------------------------------
    op.create_table(
        "license_plans",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("license_type", sa.Text(), nullable=False),
        sa.Column("billing_period", sa.Text(), nullable=False),
        sa.Column("default_entitlements", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # --- licenses (SCH §6.2) ------------------------------------------------------
    op.create_table(
        "licenses",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("plan_id", PG_UUID(as_uuid=True), sa.ForeignKey("license_plans.id"), nullable=False),
        sa.Column("parent_license_id", PG_UUID(as_uuid=True), sa.ForeignKey("licenses.id"), nullable=True),
        sa.Column("license_key_hash", sa.LargeBinary(), nullable=True),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("grace_ends_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", license_status_col, nullable=False, server_default="active"),
        sa.Column("agreement_object_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("entitlement_overrides", JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_licenses_one_effective_per_tenant", "licenses", ["tenant_id"],
        unique=True, postgresql_where=sa.text("status IN ('active', 'grace')"),
    )

    # --- license_entitlements (SCH §6.3) ------------------------------------------
    op.create_table(
        "license_entitlements",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("license_id", PG_UUID(as_uuid=True), sa.ForeignKey("licenses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entitlement_code", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),  # limit_numeric | boolean | json
        sa.Column("limit_numeric", sa.BigInteger(), nullable=True),
        sa.Column("enabled_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_json", JSONB(), nullable=True),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_license_entitlements_tenant_code", "license_entitlements", ["tenant_id", "entitlement_code"]
    )

    # --- quota_ledgers (SCH §6.4) -------------------------------------------------
    op.create_table(
        "quota_ledgers",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("license_id", PG_UUID(as_uuid=True), sa.ForeignKey("licenses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quota_code", sa.Text(), nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("limit_value", sa.BigInteger(), nullable=False),
        sa.Column("allocated_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consumed_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_quota_ledgers_period", "quota_ledgers",
        ["tenant_id", "license_id", "quota_code", "period_start"], unique=True,
    )

    # --- quota_reservations (SCH §6.5) --------------------------------------------
    op.create_table(
        "quota_reservations",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quota_ledger_id", PG_UUID(as_uuid=True), sa.ForeignKey("quota_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", reservation_status_col, nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_quota_reservations_ledger", "quota_reservations", ["quota_ledger_id", "status"])

    for table in TENANT_OWNED_TABLES:
        _enable_rls(table)

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in PLATFORM_GLOBAL_TABLES + TENANT_OWNED_TABLES:
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')


def downgrade() -> None:
    op.drop_table("quota_reservations")
    op.drop_table("quota_ledgers")
    op.drop_table("license_entitlements")
    op.drop_table("licenses")
    op.drop_table("license_plans")
    op.execute("DROP TYPE IF EXISTS quota_reservation_status")
    op.execute("DROP TYPE IF EXISTS license_status")
