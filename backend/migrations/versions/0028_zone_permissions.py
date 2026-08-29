"""Permissions for zones.

A zone is the polygon that decides where a rule applies - the restricted dock, the area in
front of a fire exit. Editing one silently changes what does and does not raise an
incident, without touching a rule, so it is `elevated` rather than ordinary editing work:
widening a zone can turn a busy walkway into a source of constant alerts, and narrowing it
can make an intrusion stop being detected at all.

Reading is separate and granted broadly, because an operator looking at an incident needs
to see the zone it fired in to make sense of it.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-28
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("zone.read", "zone", "read", "standard", "View zones and their boundaries"),
    ("zone.manage", "zone", "manage", "elevated",
     "Create, redraw and remove zones, which changes what raises an incident"),
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

    _grant(bind, "tenant_owner", "customer", ["zone.read", "zone.manage"])
    _grant(bind, "tenant_member", "customer", ["zone.read"])


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
