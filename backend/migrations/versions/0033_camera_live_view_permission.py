"""Permission for watching a camera's live feed.

`camera.read` already covers seeing a camera's metadata/status; live video is a distinct,
materially more sensitive action - it puts real, live video of a real place in front of
whoever holds the permission, not just a name and a status badge. Elevated sensitivity,
same tier as `camera.probe`/`camera.credential.manage`.

Granted to both `tenant_owner` and `tenant_member`, deliberately: watching a camera is the
single most routine day-to-day security-operations action there is - more routine than
`camera.manage` (owner-only), and matches `camera.probe`'s own existing member-level grant.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-29
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("camera.view_live", "camera", "view_live", "elevated",
     "Watch a camera's live video through the media session service"),
]

_TENANT_OWNER_GRANTS = [p[0] for p in _PERMISSIONS]
_TENANT_MEMBER_GRANTS = [p[0] for p in _PERMISSIONS]


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
