"""Adds two fixed tenant roles between the existing tenant_member/tenant_owner pair:
tenant_viewer (strictly read-only) and tenant_operator (day-to-day operations - camera
and rule management, incident triage - without user/settings/security-policy control).

No new permission codes: every grant below is an existing permissions row (site.read,
zone.read, camera.read, camera.create, camera.view_live, camera.manage, camera.probe,
rule.read, rule.manage, incident.read, incident.acknowledge, incident.assign, audit.read
- see migrations 0002/0010 and the camera/zone/rule permission migrations for where each
was first added). This migration only adds `roles` + `role_permissions` rows, following
the exact idempotent pattern 0002/0010/0037 already established.

`tenant_operator` includes `camera.create` alongside `camera.manage`, not just the
latter: the design decision this migration implements states operators can
"add/reconfigure cameras" as part of day-to-day operations, and `camera.create` (add a
new camera, migration 0010) is a distinct permission from `camera.manage` (edit an
existing one, migration 0021) - `camera.manage` alone lets an operator reconfigure a
camera that already exists but 403s on POST /api/v1/tenant/cameras itself, which doesn't
match "add cameras". Caught by scripts/e2e_finer_roles.py Step 5 actually calling that
endpoint as a real tenant_operator token, not just inspecting the JWT claim.

The resulting hierarchy: tenant_viewer (read-only) is a strict subset of tenant_member
(adds incident triage), which is a strict subset of tenant_operator (adds camera/rule
management), which is a strict subset of tenant_owner (adds user/settings/security-policy
management, per 0002's own "Full control of a tenant, including user and settings
management" description of that role).

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

# name, description, permission codes granted (all must already exist in `permissions`)
_NEW_ROLES: list[tuple[str, str, list[str]]] = [
    (
        "tenant_viewer",
        "Strictly read-only access across the tenant - no operational or management actions.",
        ["site.read", "zone.read", "camera.read", "rule.read", "incident.read", "audit.read"],
    ),
    (
        "tenant_operator",
        "Day-to-day operations: camera and rule management, live view, incident triage - "
        "without user, settings, or security-policy control.",
        [
            "site.read", "zone.read", "camera.read", "camera.create", "camera.view_live",
            "camera.manage", "camera.probe", "rule.read", "rule.manage", "incident.read",
            "incident.acknowledge", "incident.assign", "audit.read",
        ],
    ),
]


def _grant(bind, role_name: str, codes: list[str]) -> None:
    # Mirrors 0010's own `_grant` helper exactly - each migration re-defines this rather
    # than importing a previous migration file, since migration files are frozen history
    # and must not depend on each other's Python.
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :name AND audience = 'customer'"),
        {"name": role_name},
    ).scalar_one_or_none()
    if role_id is None:
        return
    for code in codes:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, effect) "
                "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
            ),
            {"r": role_id, "p": permission_id},
        )


def upgrade() -> None:
    bind = op.get_bind()

    for name, description, _codes in _NEW_ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, tenant_id, name, role_type, audience, description) "
                "VALUES (gen_random_uuid(), NULL, :name, 'system', 'customer', :description) "
                "ON CONFLICT DO NOTHING"
            ),
            {"name": name, "description": description},
        )

    for name, _description, codes in _NEW_ROLES:
        _grant(bind, name, codes)


def downgrade() -> None:
    names = tuple(name for name, *_ in _NEW_ROLES)
    op.execute(
        f"DELETE FROM role_permissions WHERE role_id IN "
        f"(SELECT id FROM roles WHERE tenant_id IS NULL AND name IN {names})"
    )
    op.execute(f"DELETE FROM roles WHERE tenant_id IS NULL AND name IN {names}")
