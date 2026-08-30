"""Permission for a reseller organization managing its own child tenants.

Granted broadly to every `tenant_owner` (customer audience), the same way
`membership.manage` (migration 0035) is - the permission alone only establishes "this
person owns *some* tenant", not "that tenant is a reseller". The business rule ("only a
reseller organization may create a child tenant") is enforced at the endpoint by checking
`organizations.organization_type`, not by which roles hold this permission - a non-reseller
tenant_owner who somehow calls it is refused with a clear `403 not_a_reseller`, not merely
missing a permission it doesn't otherwise need.

`organization.manage` (migration 0002) already covers the Admin API side (creating a
reseller organization in the first place) - no new platform-side permission needed here.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-30
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("reseller.manage_children", "reseller", "manage_children", "elevated",
     "Create and list this organization's own child (reseller_customer) tenants - only "
     "effective for an organization of type 'reseller'"),
]

_TENANT_OWNER_GRANTS = [p[0] for p in _PERMISSIONS]


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

    _grant(bind, "tenant_owner", "customer", _TENANT_OWNER_GRANTS)


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
