"""White-label branding: `org_branding` table + two SECURITY DEFINER resolver functions.

Real tenants now run under their own brand identity, not AIRIVU CSense's own (see
CHECKLIST.md's Phase 3/white-label entries for the full design). This migration adds the
data model only - no application code changes here.

**`org_branding` is a new table, not new columns on `organizations`.** `organizations`
already has its own `slug` (this migration 0001), populated at provisioning time
(`csense_shared.tenancy.provisioning`) and never shown to an end user or exposed for
admin editing - reusing it as the public URL segment would conflate an internal
identifier with a public brand handle that only some orgs ever get. A separate table
means "is this org branded at all" is a single EXISTS check, not several independently-
managed nullable columns on the platform's core identity table.

**Why two functions sharing one recursive walk, not one function or two independent
queries:** `org_branding_resolve(organization_id)` is the authenticated, post-login
path - it walks `organization_relationships` upward (mirroring `reseller_child_
tenant_rollup()`'s own `status = 'active'` join, migration 0056) until it finds the
nearest ancestor with a branding row, so a reseller's sub-customer with no branding of
its own inherits the reseller's look. `org_branding_resolve_by_slug(slug)` is the public,
pre-login path - a one-line wrapper that resolves slug -> organization_id and calls the
function above, so the inheritance logic exists in exactly one place rather than being
copied into a second query.

**Why SECURITY DEFINER here, when neither `organizations` nor `organization_
relationships` carries RLS** (confirmed directly against the live schema this session -
only `memberships`/`membership_resource_scopes`/`audit_events` do, per migration 0001).
Unlike `reseller_child_tenant_rollup()`, this isn't crossing an RLS boundary - it's
**column scoping**: a narrow, fixed `RETURNS TABLE` is a much smaller grant than handing
the tenant-facing `csense_api` role broad `SELECT` on `organizations`/`organization_
relationships` outright, which matters specifically because one of the two entry points
(`org_branding_resolve_by_slug`) is reachable from a fully unauthenticated request.

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-19
"""
from __future__ import annotations

import os
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def _app_roles() -> list[str]:
    # Same env-var-driven pair every migration since 0006 uses for post-0004 GRANTs.
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


_RESOLVE_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION org_branding_resolve(p_organization_id uuid)
    RETURNS TABLE (
        source_organization_id uuid,
        depth int,
        slug text,
        display_name text,
        logo_object_id uuid,
        favicon_object_id uuid,
        color_primary text,
        color_accent text,
        color_canvas text,
        email_from_name text
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
        WITH RECURSIVE ancestors(organization_id, depth) AS (
            SELECT p_organization_id, 0
            UNION ALL
            SELECT rel.parent_organization_id, a.depth + 1
            FROM ancestors a
            JOIN organization_relationships rel
              ON rel.child_organization_id = a.organization_id
             AND rel.status = 'active'
            WHERE a.depth < 10
        )
        SELECT a.organization_id, a.depth, ob.slug, ob.display_name, ob.logo_object_id,
               ob.favicon_object_id, ob.color_primary, ob.color_accent, ob.color_canvas,
               ob.email_from_name
        FROM ancestors a
        JOIN org_branding ob ON ob.organization_id = a.organization_id
        WHERE ob.status = 'active'
        ORDER BY a.depth ASC
        LIMIT 1
    $$
"""

_RESOLVE_BY_SLUG_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION org_branding_resolve_by_slug(p_slug text)
    RETURNS TABLE (
        source_organization_id uuid,
        depth int,
        slug text,
        display_name text,
        logo_object_id uuid,
        favicon_object_id uuid,
        color_primary text,
        color_accent text,
        color_canvas text,
        email_from_name text
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
        SELECT * FROM org_branding_resolve(
            (SELECT organization_id FROM org_branding WHERE slug = p_slug AND status = 'active')
        )
    $$
"""


def upgrade() -> None:
    op.create_table(
        "org_branding",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", PG_UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        # Plain text, not citext: the CHECK constraint below already forces pure
        # lowercase, so a case-insensitive column type would only add a confusing
        # mismatch (a case-sensitive constraint on a case-insensitive column) with no
        # real benefit - two slugs differing only by case can never both exist anyway.
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("logo_object_id", PG_UUID(as_uuid=True), sa.ForeignKey("stored_objects.id"), nullable=True),
        sa.Column("favicon_object_id", PG_UUID(as_uuid=True), sa.ForeignKey("stored_objects.id"), nullable=True),
        sa.Column("color_primary", sa.Text(), nullable=True),
        sa.Column("color_accent", sa.Text(), nullable=True),
        sa.Column("color_canvas", sa.Text(), nullable=True),
        sa.Column("email_from_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_org_branding_status"),
        # A slug is a URL path segment on a case-sensitive route table - lowercase only,
        # matching the admin-facing validation regex in branding.py exactly, so a slug
        # can never be inserted by any path (API or direct SQL) that the frontend's
        # router would then fail to match case-sensitively.
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,48}$'", name="ck_org_branding_slug_format"),
    )

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON org_branding TO "{role}"')

    # --- permission + platform_admin grant, same shape as 0056's own new permission ---
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
            "VALUES (:id, 'organization.branding.manage', 'organization.branding', 'manage', "
            "'elevated', 'Manage an organization''s white-label branding (platform)') "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"id": uuid.uuid4()},
    )
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'")
    ).scalar_one()
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'organization.branding.manage'")
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id, effect) "
            "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
        ),
        {"r": role_id, "p": permission_id},
    )

    # --- resolver functions ---
    op.execute(_RESOLVE_FUNCTION_SQL)
    op.execute(_RESOLVE_BY_SLUG_FUNCTION_SQL)
    for func_sig in ("org_branding_resolve(uuid)", "org_branding_resolve_by_slug(text)"):
        op.execute(f"REVOKE ALL ON FUNCTION {func_sig} FROM PUBLIC")
        op.execute(f'GRANT EXECUTE ON FUNCTION {func_sig} TO "csense_api"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS org_branding_resolve_by_slug(text)")
    op.execute("DROP FUNCTION IF EXISTS org_branding_resolve(uuid)")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id = "
        "(SELECT id FROM permissions WHERE code = 'organization.branding.manage')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'organization.branding.manage'")
    op.drop_table("org_branding")
