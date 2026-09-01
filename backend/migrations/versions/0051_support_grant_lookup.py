"""A narrow, privileged read for support-grant elevation.

`current_tenant_context` (backend/tenant_api/app/deps.py) needs to check, for a
platform-audience token naming a target tenant, whether that developer has an active,
unexpired support grant against that exact tenant - *before* any tenant RLS context
exists to make the row visible the ordinary way (neither RLS branch on `support_grants`
applies yet: `app.tenant_id` isn't set, and the Tenant API's own database role isn't a
`csense_platform` member so `app.is_platform` wouldn't help even if set). The same shape
`csense_active_membership_for_user()` (migration 0005) already established for the
equivalent pre-tenant-context problem at login.

Deliberately narrow, the same reasoning `edge_vpn_pool_snapshot()` (migration 0029) gives
for its own SECURITY DEFINER hole:

  Returns only `id` and a computed `permissions` array - never `purpose`, `ticket_reference`,
  or `resource_scope`. A lookup used to mint request-time authorization has no legitimate
  reason to leak the human-readable justification text back into request handling.

  `permissions` is not a raw echo of `support_grants.requested_scopes` - it is that array
  intersected, inside this function, against `permissions`/`role_permissions`/`roles` for
  codes actually granted (`role_permissions.effect = 'allow'`, the same `allowed - denied`
  distinction `identity.py`'s `get_role_permissions` already draws) to some `customer`-
  audience role. A typo'd or stale scope string on the grant (nothing validates
  `requested_scopes` against real permission codes at request time - see
  `backend/admin_api/app/api/support.py`) must never silently become a real permission on
  an elevated `TenantContext`; this is where that gets caught.

  `expires_at > now()` is checked directly in the WHERE clause rather than trusting the
  `status` column alone - the same lazy-expiry precedent `support.py`'s own reads already
  established (`status` can lag "now" by up to the time until the next explicit read) -
  so elevation can never briefly outlive the grant's own real expiry.

  `search_path` is pinned, for the same reason `edge_vpn_pool_snapshot()` states: without
  it, a caller able to create objects could shadow `support_grants`/`permissions` with
  their own table and have this function read it with the owner's privileges.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-01
"""
from __future__ import annotations

import os

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def _api_role() -> str:
    return os.environ.get("POSTGRES_API_USER", "csense_api")


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION support_grant_lookup(p_developer_user_id uuid, p_tenant_id uuid)
        RETURNS TABLE (grant_id uuid, permissions text[])
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT g.id,
                   COALESCE(
                       ARRAY(
                           SELECT DISTINCT p.code
                           FROM unnest(g.requested_scopes) AS scope(code)
                           JOIN permissions p ON p.code = scope.code
                           JOIN role_permissions rp ON rp.permission_id = p.id AND rp.effect = 'allow'
                           JOIN roles r ON r.id = rp.role_id AND r.audience = 'customer'
                       ),
                       ARRAY[]::text[]
                   )
            FROM support_grants g
            WHERE g.developer_user_id = p_developer_user_id
              AND g.tenant_id = p_tenant_id
              AND g.status = 'active'
              AND g.expires_at > now()
            ORDER BY g.created_at DESC
            LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION support_grant_lookup(uuid, uuid) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION support_grant_lookup(uuid, uuid) TO "{_api_role()}"'
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS support_grant_lookup(uuid, uuid)")
