"""Extends reseller_child_tenant_rollup() (migration 0056) with two trailing columns,
list_price_cents and currency, sourced from the child tenant's current active/grace
license's plan (license_plans.price_cents/currency, added by migration 0058 - immediately
prior to this one). This is the real schema behind the "list-price rollup" this session's
plan settled on: no payment/invoicing system exists to know what was actually billed, so
the honest thing a rollup can sum is each child's current plan's sticker price - see
0058's own docstring for the full reasoning.

**Correction to this session's own plan**: the plan assumed `CREATE OR REPLACE FUNCTION`
could add a trailing return column without a `DROP FUNCTION` first, reasoning by analogy
to 0055's note about DROP+CREATE being needed only when *removing/reordering* columns.
That analogy doesn't hold here - verified against the real Postgres error when this
migration was first run against the live stack:
`psycopg.errors.InvalidFunctionDefinition: cannot change return type of existing
function / DETAIL: Row type defined by OUT parameters is different.` A `RETURNS TABLE`
function is implemented via OUT parameters, and Postgres's own documented rule for
`CREATE OR REPLACE FUNCTION` is stricter than the plan assumed: "you cannot change the
types of any OUT parameters except by dropping the function" - which covers *adding* one,
not just removing/reordering. So this migration does a real `DROP FUNCTION IF EXISTS`
before recreating with the two extra columns, same as 0055 already does for its own
column-removing case. Both new columns are NULL when a child tenant has no active/grace
license, or when it does but that license's plan has no price on record (an existing plan
predating migration 0058) - matching the "NULL means unknown, not zero" contract
price_cents itself already establishes.

Grants are re-issued after CREATE OR REPLACE - matching this codebase's own established
convention (see migration 0056's own comment on this) that GRANT/REVOKE are not assumed
to persist reliably across a function replace, so every function migration re-issues them
explicitly rather than relying on carryover.

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

_OLD_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION reseller_child_tenant_rollup(p_parent_organization_id uuid)
    RETURNS TABLE (
        tenant_id uuid,
        display_name text,
        tenant_status text,
        site_count bigint,
        camera_count bigint,
        active_incident_count bigint,
        license_status text
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
        SELECT
            t.id,
            o.display_name,
            t.status,
            (SELECT count(*) FROM sites s WHERE s.tenant_id = t.id AND s.deleted_at IS NULL),
            (SELECT count(*) FROM cameras c WHERE c.tenant_id = t.id AND c.deleted_at IS NULL),
            (
                SELECT count(*) FROM incidents i
                WHERE i.tenant_id = t.id
                  AND i.status IN ('open', 'acknowledged', 'investigating', 'escalated')
            ),
            (
                SELECT l.status::text FROM licenses l
                WHERE l.tenant_id = t.id AND l.status IN ('active', 'grace')
                ORDER BY l.created_at DESC LIMIT 1
            )
        FROM organization_relationships rel
        JOIN organizations o ON o.id = rel.child_organization_id
        JOIN tenants t ON t.organization_id = o.id
        WHERE rel.parent_organization_id = p_parent_organization_id
          AND rel.status = 'active'
        ORDER BY t.created_at DESC
    $$
"""

_NEW_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION reseller_child_tenant_rollup(p_parent_organization_id uuid)
    RETURNS TABLE (
        tenant_id uuid,
        display_name text,
        tenant_status text,
        site_count bigint,
        camera_count bigint,
        active_incident_count bigint,
        license_status text,
        list_price_cents bigint,
        currency text
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
        SELECT
            t.id,
            o.display_name,
            t.status,
            (SELECT count(*) FROM sites s WHERE s.tenant_id = t.id AND s.deleted_at IS NULL),
            (SELECT count(*) FROM cameras c WHERE c.tenant_id = t.id AND c.deleted_at IS NULL),
            (
                SELECT count(*) FROM incidents i
                WHERE i.tenant_id = t.id
                  AND i.status IN ('open', 'acknowledged', 'investigating', 'escalated')
            ),
            (
                SELECT l.status::text FROM licenses l
                WHERE l.tenant_id = t.id AND l.status IN ('active', 'grace')
                ORDER BY l.created_at DESC LIMIT 1
            ),
            (
                SELECT p.price_cents FROM licenses l JOIN license_plans p ON p.id = l.plan_id
                WHERE l.tenant_id = t.id AND l.status IN ('active', 'grace')
                ORDER BY l.created_at DESC LIMIT 1
            ),
            (
                SELECT p.currency FROM licenses l JOIN license_plans p ON p.id = l.plan_id
                WHERE l.tenant_id = t.id AND l.status IN ('active', 'grace')
                ORDER BY l.created_at DESC LIMIT 1
            )
        FROM organization_relationships rel
        JOIN organizations o ON o.id = rel.child_organization_id
        JOIN tenants t ON t.organization_id = o.id
        WHERE rel.parent_organization_id = p_parent_organization_id
          AND rel.status = 'active'
        ORDER BY t.created_at DESC
    $$
"""


def upgrade() -> None:
    # See this migration's own docstring: CREATE OR REPLACE cannot add a column to a
    # RETURNS TABLE function's OUT-parameter row type - a real DROP is required first.
    op.execute("DROP FUNCTION IF EXISTS reseller_child_tenant_rollup(uuid)")
    op.execute(_NEW_FUNCTION_SQL)
    op.execute("REVOKE ALL ON FUNCTION reseller_child_tenant_rollup(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION reseller_child_tenant_rollup(uuid) TO "csense_api"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reseller_child_tenant_rollup(uuid)")
    op.execute(_OLD_FUNCTION_SQL)
    op.execute("REVOKE ALL ON FUNCTION reseller_child_tenant_rollup(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION reseller_child_tenant_rollup(uuid) TO "csense_api"')
