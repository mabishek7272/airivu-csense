"""Adds platform_developers (SCH §5.9) and seeds baseline permissions + system roles.

Permissions/roles are reference data, not secrets, so they are safe to seed in a
migration (idempotent — re-running is a no-op via ON CONFLICT). Actual user accounts are
never created here; see backend/scripts/seed_dev_data.py for local smoke-test accounts.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# code, resource, action, risk_level, description
_PERMISSIONS: list[tuple[str, str, str, str, str]] = [
    ("tenant.user.manage", "tenant.user", "manage", "elevated", "Invite/manage users within a tenant"),
    ("tenant.settings.manage", "tenant.settings", "manage", "standard", "Manage tenant settings"),
    ("audit.read", "audit", "read", "standard", "Read audit events"),
    ("audit.export", "audit", "export", "elevated", "Export audit events"),
    ("support.request", "support", "request", "elevated", "Request a just-in-time support grant"),
    ("support.access", "support", "access", "elevated", "Use an approved support grant"),
    ("security.policy.manage", "security.policy", "manage", "elevated", "Manage tenant security policy"),
    ("organization.manage", "organization", "manage", "elevated", "Manage organizations (platform)"),
    ("tenant.manage", "tenant", "manage", "elevated", "Manage any tenant (platform)"),
    ("license.manage", "license", "manage", "elevated", "Manage licenses/entitlements (platform)"),
]

# name, audience, description, permission codes granted
_SYSTEM_ROLES: list[tuple[str, str, str, list[str]]] = [
    (
        "tenant_owner",
        "customer",
        "Full control of a tenant, including user and settings management.",
        ["tenant.user.manage", "tenant.settings.manage", "audit.read", "audit.export", "security.policy.manage"],
    ),
    (
        "tenant_member",
        "customer",
        "Standard tenant member with read access.",
        ["audit.read"],
    ),
    (
        "platform_admin",
        "platform",
        "Full platform operator with cross-tenant administration rights.",
        ["organization.manage", "tenant.manage", "license.manage", "audit.read", "audit.export", "support.access"],
    ),
]


def upgrade() -> None:
    op.create_table(
        "platform_developers",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("primary_team", sa.Text(), nullable=True),
        sa.Column("manager_user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("access_review_due_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    bind = op.get_bind()
    permissions_table = sa.table(
        "permissions",
        sa.column("id", PG_UUID(as_uuid=True)),
        sa.column("code", sa.Text),
        sa.column("resource", sa.Text),
        sa.column("action", sa.Text),
        sa.column("risk_level", sa.Text),
        sa.column("description", sa.Text),
    )
    roles_table = sa.table(
        "roles",
        sa.column("id", PG_UUID(as_uuid=True)),
        sa.column("tenant_id", PG_UUID(as_uuid=True)),
        sa.column("name", sa.Text),
        sa.column("role_type", sa.Text),
        sa.column("audience", sa.Text),
        sa.column("description", sa.Text),
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", PG_UUID(as_uuid=True)),
        sa.column("permission_id", PG_UUID(as_uuid=True)),
        sa.column("effect", sa.Text),
    )

    permission_ids: dict[str, uuid.UUID] = {}
    for code, resource, action, risk_level, description in _PERMISSIONS:
        permission_id = uuid.uuid4()
        permission_ids[code] = permission_id
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, resource, action, risk_level, description)
                VALUES (:id, :code, :resource, :action, :risk_level, :description)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {
                "id": permission_id,
                "code": code,
                "resource": resource,
                "action": action,
                "risk_level": risk_level,
                "description": description,
            },
        )

    for name, audience, description, codes in _SYSTEM_ROLES:
        role_id = uuid.uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO roles (id, tenant_id, name, role_type, audience, description)
                VALUES (:id, NULL, :name, 'system', :audience, :description)
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": role_id, "name": name, "audience": audience, "description": description},
        )
        existing = bind.execute(
            sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :name"), {"name": name}
        ).scalar_one()
        for code in codes:
            perm_row = bind.execute(
                sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
            ).scalar_one()
            bind.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id, effect)
                    VALUES (:role_id, :permission_id, 'allow')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"role_id": existing, "permission_id": perm_row},
            )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions")
    op.execute("DELETE FROM roles WHERE tenant_id IS NULL AND name IN ('tenant_owner', 'tenant_member', 'platform_admin')")
    op.execute(
        "DELETE FROM permissions WHERE code IN ("
        "'tenant.user.manage','tenant.settings.manage','audit.read','audit.export',"
        "'support.request','support.access','security.policy.manage',"
        "'organization.manage','tenant.manage','license.manage')"
    )
    op.drop_table("platform_developers")
