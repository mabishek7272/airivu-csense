"""Permission for camera/NVR discovery.

Same sensitivity tier as `camera.probe` (migration 0021) and for the same reason: this
makes the server open a connection to an address the caller supplied (NVR channel listing)
or, once an edge agent exists, triggers a scan of a customer's own network - a capability
worth naming and granting explicitly rather than implying from "can create a camera".

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-29
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("camera.discover", "camera", "discover", "elevated",
     "Discover NVR channels or (once available) ONVIF devices on a site's network"),
]

_TENANT_OWNER_GRANTS = [p[0] for p in _PERMISSIONS]
# Matches camera.probe's own precedent: a member can diagnose/discover, but editing what a
# camera is (camera.manage) or its credentials stays a separate, narrower grant.
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
