"""Permissions for the Edge section.

Split so that issuing an enrolment token is its own capability. Adding a device record is
paperwork; issuing the token that turns an unconfigured box into a trusted member of the
tenant's network is not, and someone who can tidy the device list should not automatically
be able to mint one.

`edge.enrol` is marked elevated for the same reason: an enrolment token, once redeemed,
gives a device an identity that can push detections and receive a VPN address.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-27
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("edge.read", "edge", "read", "standard",
     "View edge devices, their hardware and their health"),
    ("edge.manage", "edge", "manage", "standard",
     "Add, edit and remove edge device records"),
    ("edge.enrol", "edge", "enrol", "elevated",
     "Issue enrolment tokens that let a device claim an identity"),
]

_TENANT_OWNER_GRANTS = [p[0] for p in _PERMISSIONS]
_TENANT_MEMBER_GRANTS = ["edge.read"]


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
    _grant(bind, "tenant_member", "customer", _TENANT_MEMBER_GRANTS)


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
