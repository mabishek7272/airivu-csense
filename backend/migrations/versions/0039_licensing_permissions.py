"""Permissions for the licensing/quota foundation (migration 0038).

`license.manage` (platform_admin only) covers both reading and writing plans/licenses on
the Admin API side - the same simplicity `organization.manage` already uses for its own
GET+POST pair, rather than splitting read/write for a surface this narrow.

`license.read` is a plain informational read of a tenant's own effective entitlements and
current quota usage - low risk, granted to both `tenant_owner` and `tenant_member` (unlike
`membership.manage`/`reseller.manage_children`, which stay owner-only because they can
change who has access to what).

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-30
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("license.manage", "license", "manage", "elevated",
     "Create license plans and issue/inspect licenses for any tenant (platform-only)"),
    ("license.read", "license", "read", "standard",
     "View this tenant's own effective entitlements and current quota usage"),
]


def _grant(bind, role_name: str, audience: str, codes: list[str]) -> None:
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :n AND audience = :a"),
        {"n": role_name, "a": audience},
    ).scalar_one_or_none()
    if role_id is None:
        return
    for code in codes:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :c"), {"c": code}
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

    for code, resource, action, risk, description in _PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
                "VALUES (:id, :code, :resource, :action, :risk, :desc) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(), "code": code, "resource": resource,
                "action": action, "risk": risk, "desc": description,
            },
        )

    _grant(bind, "platform_admin", "platform", ["license.manage"])
    _grant(bind, "tenant_owner", "customer", ["license.read"])
    _grant(bind, "tenant_member", "customer", ["license.read"])


def downgrade() -> None:
    bind = op.get_bind()
    codes = [p[0] for p in _PERMISSIONS]
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": codes},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": codes}
    )
