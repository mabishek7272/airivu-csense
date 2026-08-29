"""Permissions for recipient groups and notification policies.

Both decide who gets woken at 3am and how - which makes them `elevated`, the same
reasoning as `zone.manage`: a recipient group with the wrong phone number, or a policy
whose escalation ladder skips a step, fails silently. Nobody sees a missing alert; they
just never get called. Reading is separate and broad, because an operator reviewing an
incident's notification history needs to see who a step was meant to reach.

One permission pair covers both resources rather than four separate ones - they are edited
together in practice (a policy step names recipient groups by id) and splitting them would
not stop anyone from doing anything, only make the grant list longer.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-29
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("notification.read", "notification", "read", "standard",
     "View recipient groups and notification policies"),
    ("notification.manage", "notification", "manage", "elevated",
     "Create, edit and remove recipient groups and notification policies - "
     "decides who is told about an incident and how"),
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

    _grant(bind, "tenant_owner", "customer", ["notification.read", "notification.manage"])
    _grant(bind, "tenant_member", "customer", ["notification.read"])


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
