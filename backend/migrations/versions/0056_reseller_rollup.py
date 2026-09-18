"""Adds reseller.view_rollup (a new permission - a rollup is operational/business data,
not an audit trail, so it gets its own permission rather than reusing audit.read) and
reseller_child_tenant_rollup(), a narrow SECURITY DEFINER function crossing the RLS
boundary the same way edge_vpn_pool_snapshot() (migration 0029) and
support_grant_lookup() (migration 0051) already do.

Why this needs SECURITY DEFINER at all: cameras/incidents/sites all carry strict
per-tenant RLS (tenant_id = current_setting('app.tenant_id')::uuid, no exception for "I
am this tenant's reseller parent" - see this plan's own "Before you start" section). An
ordinary query for "how many cameras does each child tenant have", run under the
reseller's own tenant session, would return every child tenant with a count of zero -
not an error, silently wrong - the exact failure mode this codebase's own reseller.py
docstring already documents having hit once, on the child-tenant *list* itself (fixed
there by never crossing the RLS boundary; a rollup, unlike a list of tenant identities,
genuinely needs real numbers from inside each child tenant, so it can't take that same
"just don't join across it" escape).

The function takes a single parameter (the caller's own organization id, not
attacker-suppliable per-child ids) and returns only COUNTS - no incident titles, no
camera names, no anything that would leak a child tenant's actual operational detail to
its reseller parent beyond what organization_relationships already establishes they're
entitled to see exists. Every count is scoped by joining back through
organization_relationships WHERE parent_organization_id = the caller's own id AND
status = 'active' - the same authorization boundary _LIST_SQL (reseller.py) already
uses for the child-tenant list itself, so a rollup can never return more tenants than
the existing list endpoint would.

Note on the "active incidents" status set: the plan draft for this migration originally
guessed `status NOT IN ('closed', 'dismissed')`. Verified against the real
incidents.status enum (`INCIDENT_STATUS` in migration 0009 -
["open", "acknowledged", "investigating", "escalated", "resolved", "dismissed"]) and
against `backend/tenant_api/app/api/incidents.py`'s own `ACTIVE_STATUSES` /
`VALID_STATUSES`, which is what `list_incidents`'s `status=active` query param already
resolves to: `ACTIVE_STATUSES = ("open", "acknowledged", "investigating", "escalated")`.
There is no `'closed'` value in the enum at all (casting the literal 'closed' to
incident_status would fail outright), and the real "active" set deliberately excludes
`'resolved'` too. Corrected here to
`status IN ('open', 'acknowledged', 'investigating', 'escalated')` so the rollup's
"active incidents" count means the same thing the rest of the product already means by
that word, instead of re-guessing a different definition.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-18
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
            "VALUES (:id, 'reseller.view_rollup', 'reseller', 'view_rollup', 'standard', "
            "'View an aggregate rollup across a reseller''s child tenants') "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"id": uuid.uuid4()},
    )
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'")
    ).scalar_one()
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'reseller.view_rollup'")
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id, effect) "
            "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
        ),
        {"r": role_id, "p": permission_id},
    )

    op.execute(
        """
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
    )
    op.execute("REVOKE ALL ON FUNCTION reseller_child_tenant_rollup(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION reseller_child_tenant_rollup(uuid) TO "csense_api"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reseller_child_tenant_rollup(uuid)")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code = 'reseller.view_rollup')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'reseller.view_rollup'")
