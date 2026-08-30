"""Permission for a tenant's own dashboard summary (TRD §10.2's own representative
endpoint, `GET /api/v1/tenant/dashboard`, previously unbuilt - and the "empty dashboard"
step of the Phase 2 vertical-slice test).

Granted broadly to both `tenant_owner` and `tenant_member`, the same tier as
`license.read` (migration 0039) - a read-only summary of counts this tenant already has
individual permission to see the detail of (`site.read`, `camera.manage`, `incident.
read`), not a new access grant in its own right.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-30
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("dashboard.read", "dashboard", "read", "standard",
     "View this tenant's own dashboard summary (site/camera/incident counts)"),
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

    _grant(bind, "tenant_owner", "customer", ["dashboard.read"])
    _grant(bind, "tenant_member", "customer", ["dashboard.read"])


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
