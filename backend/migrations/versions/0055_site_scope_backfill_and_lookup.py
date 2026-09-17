"""Two things, both required before site-scope enforcement can ship safely:

1. Data backfill: every currently-*active* membership with site_scope_mode='none' is set
   to 'all'. Today, with zero enforcement anywhere, a 'none'-scoped active membership can
   in practice see every site in its tenant - the enum value has never actually meant
   "sees nothing" for anyone using the product so far (see the invite form's own
   "No sites (assign later)" label for the intended, but until-now unenforced, meaning).
   Flipping enforcement on without this backfill would silently lock out every such
   membership the instant this deploys. Only 'invited'/'suspended'/'revoked' memberships
   are left alone - they have no current visibility to preserve, and a fresh invite from
   this point forward gets the real, enforced 'none' default (real lockout until a site is
   assigned), matching the UI's original intent for new invitations specifically.

2. csense_active_membership_for_user() (migration 0005) is extended to also return
   site_scope_mode and a site_ids array - it's the SECURITY DEFINER function login()/
   refresh() call before any tenant scope is set, so it's the only place that can resolve
   a user's own site scope pre-auth (the same reasoning 0005's own docstring gives for why
   it exists as SECURITY DEFINER at all: an ordinary RLS-scoped query can't run yet,
   there's no tenant context to scope it to). site_ids reads membership_resource_scopes
   where resource_type='site' and effect='allow' - the only kind of row this feature ever
   writes to that table (a narrower building block than the schema allows; 'deny' rows and
   non-site resource_types are out of scope for this pass, matching the plan's stated File
   Structure boundary).

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE memberships SET site_scope_mode = 'all', updated_at = now() "
        "WHERE site_scope_mode = 'none' AND status = 'active'"
    )

    # DROP + CREATE (not CREATE OR REPLACE) because the return type is changing - Postgres
    # refuses to REPLACE a function with a different RETURNS TABLE shape.
    op.execute("DROP FUNCTION IF EXISTS csense_active_membership_for_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION csense_active_membership_for_user(p_user_id uuid)
        RETURNS TABLE (
            membership_id uuid,
            tenant_id uuid,
            role_id uuid,
            site_scope_mode text,
            site_ids uuid[]
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        STABLE
        AS $$
            SELECT
                m.id,
                m.tenant_id,
                m.role_id,
                m.site_scope_mode::text,
                COALESCE(
                    (
                        SELECT array_agg(mrs.resource_id)
                        FROM membership_resource_scopes mrs
                        WHERE mrs.membership_id = m.id
                          AND mrs.resource_type = 'site'
                          AND mrs.effect = 'allow'
                    ),
                    ARRAY[]::uuid[]
                )
            FROM memberships m
            WHERE m.user_id = p_user_id AND m.status = 'active'
            ORDER BY m.created_at
            LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION csense_active_membership_for_user(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION csense_active_membership_for_user(uuid) TO "csense_api"')


def downgrade() -> None:
    # The data backfill is not reversible (which specific rows were 'none' before upgrade
    # is not recorded) - downgrade restores the function shape only, matching how this
    # codebase's other backfill-carrying migrations already treat downgrade as
    # best-effort schema reversal, not a full data-state undo.
    op.execute("DROP FUNCTION IF EXISTS csense_active_membership_for_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION csense_active_membership_for_user(p_user_id uuid)
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
    op.execute('GRANT EXECUTE ON FUNCTION csense_active_membership_for_user(uuid) TO "csense_api"')
