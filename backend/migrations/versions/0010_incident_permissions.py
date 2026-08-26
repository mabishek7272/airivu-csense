"""Incident and site permissions, granted to the tenant roles.

Permission codes follow `resource.action` (TRD §7.3), which names `incident.read`,
`incident.acknowledge`, `incident.close` and `evidence.download` explicitly.

The split between `tenant_owner` and `tenant_member` matters: a member should be able to
work the incident queue - read, acknowledge, investigate - without being able to dismiss
or reconfigure the tenant. Closing an incident is a judgement about whether something
real happened, so it sits with the owner role by default.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-26
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("incident.read", "incident", "read", "standard", "View incidents and their history"),
    ("incident.acknowledge", "incident", "acknowledge", "standard", "Acknowledge an incident"),
    ("incident.assign", "incident", "assign", "standard", "Assign an incident to a user"),
    ("incident.close", "incident", "close", "elevated", "Resolve or dismiss an incident"),
    ("evidence.download", "evidence", "download", "elevated", "Download incident evidence"),
    ("site.read", "site", "read", "standard", "View sites and zones"),
    ("site.manage", "site", "manage", "elevated", "Create and modify sites and zones"),
    ("camera.read", "camera", "read", "standard", "View cameras"),
    ("camera.create", "camera", "create", "elevated", "Add cameras"),
]

_OWNER_GRANTS = [code for code, *_ in _PERMISSIONS]
_MEMBER_GRANTS = [
    "incident.read",
    "incident.acknowledge",
    "incident.assign",
    "site.read",
    "camera.read",
]


def _grant(bind, role_name: str, codes: list[str]) -> None:
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

    for code, resource, action, risk_level, description in _PERMISSIONS:
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, resource, action, risk_level, description)
                VALUES (:id, :code, :resource, :action, :risk, :desc)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4(),
                "code": code,
                "resource": resource,
                "action": action,
                "risk": risk_level,
                "desc": description,
            },
        )

    _grant(bind, "tenant_owner", _OWNER_GRANTS)
    _grant(bind, "tenant_member", _MEMBER_GRANTS)


def downgrade() -> None:
    codes = tuple(code for code, *_ in _PERMISSIONS)
    op.execute(
        f"DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN {codes})"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN {codes}")
