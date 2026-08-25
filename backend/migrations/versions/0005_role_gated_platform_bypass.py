"""Ties the platform RLS bypass to the database role, not just a session flag.

Problem with 0001's policies: the platform escape hatch was
`current_setting('app.is_platform')`, and *any* role can set a custom GUC. A SQL
injection anywhere in the Tenant API could set that flag and read every tenant's rows —
the isolation boundary would rest entirely on application code never being wrong.

SCH §15 calls for "a separate restricted platform role" for the Admin API, so the bypass
now requires BOTH:
  1. membership in the `csense_platform` group role (the Tenant API's login role is not
     a member, so it cannot bypass at all — flag or no flag), AND
  2. the explicit per-transaction `app.is_platform` opt-in (so even the Admin API only
     sees cross-tenant rows in code paths that deliberately asked for it).

Also adds a narrow SECURITY DEFINER lookup for the one legitimate cross-tenant read the
Tenant API needs: resolving which tenant a user belongs to at login, before any tenant
context exists. That is exposed as a single-purpose function rather than blanket bypass.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25
"""
from __future__ import annotations

import os

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

PLATFORM_GROUP_ROLE = "csense_platform"
RLS_TABLES = ("memberships", "membership_resource_scopes")


def _api_role() -> str:
    return os.environ.get("POSTGRES_API_USER", "csense_api")


def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR (
                    coalesce(current_setting('app.is_platform', true)::boolean, false)
                    AND pg_has_role(current_user, '{PLATFORM_GROUP_ROLE}', 'MEMBER')
                )
            )
            WITH CHECK (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR (
                    coalesce(current_setting('app.is_platform', true)::boolean, false)
                    AND pg_has_role(current_user, '{PLATFORM_GROUP_ROLE}', 'MEMBER')
                )
            )
            """
        )

    op.execute("DROP POLICY IF EXISTS audit_events_tenant_isolation ON audit_events")
    op.execute(
        f"""
        CREATE POLICY audit_events_tenant_isolation ON audit_events
        USING (
            (tenant_id IS NOT NULL AND tenant_id = current_setting('app.tenant_id', true)::uuid)
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{PLATFORM_GROUP_ROLE}', 'MEMBER')
            )
        )
        WITH CHECK (
            (tenant_id IS NOT NULL AND tenant_id = current_setting('app.tenant_id', true)::uuid)
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{PLATFORM_GROUP_ROLE}', 'MEMBER')
            )
        )
        """
    )

    # The single legitimate pre-tenant-context cross-tenant read: at login we know the
    # user but not yet the tenant. SECURITY DEFINER runs as the function owner, so it can
    # see past RLS — deliberately scoped to one user's own active membership and nothing
    # else. `search_path` is pinned to defeat search-path hijacking.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION csense_active_membership_for_user(p_user_id uuid)
        RETURNS TABLE (membership_id uuid, tenant_id uuid, role_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        STABLE
        AS $$
            SELECT m.id, m.tenant_id, m.role_id
            FROM memberships m
            WHERE m.user_id = p_user_id AND m.status = 'active'
            ORDER BY m.created_at
            LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION csense_active_membership_for_user(uuid) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION csense_active_membership_for_user(uuid) TO "{_api_role()}"'
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS csense_active_membership_for_user(uuid)")

    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
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

    op.execute("DROP POLICY IF EXISTS audit_events_tenant_isolation ON audit_events")
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
