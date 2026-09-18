"""membership.read: lets any tenant member see their own team roster, separate from
membership.manage (invite/edit/revoke), which stays tenant_owner-only per its own
migration 0035 reasoning ("the ability to add a person to the tenant and decide what
they can do is the thing every other permission in this system is downstream of").
Reading who else is on the team carries none of that risk - mirrors the "safe sharing"
pattern migration 0053 established for diagnostic.read: same permission code, granted
broadly, no split between platform/customer variants needed.

Granted to all four customer roles (tenant_owner, tenant_operator, tenant_member,
tenant_viewer - the latter two added this session by migration 0054) - a viewer who
can see everything else read-only in the tenant should reasonably see who's on the
team too.

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-18
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("membership.read", "membership", "read", "standard",
     "View this tenant's own team roster"),
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

    _grant(bind, "tenant_owner", "customer", ["membership.read"])
    _grant(bind, "tenant_operator", "customer", ["membership.read"])
    _grant(bind, "tenant_member", "customer", ["membership.read"])
    _grant(bind, "tenant_viewer", "customer", ["membership.read"])


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
