"""Permissions for editing cameras, and a separate one for their credentials.

`camera.create` and `camera.read` already existed. This adds the rest of the lifecycle,
and splits one capability out deliberately:

**`camera.credential.manage` is not the same permission as `camera.manage`.** Renaming a
camera, moving it between zones or disabling it is routine work a site supervisor should
be able to do. Replacing the stored RTSP password is not: it is the credential to a device
that watches people, and whoever holds it can point our connection wherever they like.
Bundling the two would mean every person who can tidy up a camera list can also rewrite
those credentials, which is more access than the job needs.

**`camera.probe` is separate too**, because probing makes the server open an outbound
connection to an address the caller supplies. That is a capability worth naming and
granting explicitly rather than implying from "can edit a camera".

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-27
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("camera.manage", "camera", "manage", "standard",
     "Edit and remove cameras, zones and stream settings"),
    ("camera.credential.manage", "camera.credential", "manage", "elevated",
     "Set or replace the stored credentials a camera connects with"),
    ("camera.probe", "camera", "probe", "elevated",
     "Make the server connect to a camera to verify its stream"),
]

_TENANT_OWNER_GRANTS = [p[0] for p in _PERMISSIONS]
# A member can look, and can probe to diagnose "why is this camera offline" - but cannot
# change what a camera is or what it authenticates with.
_TENANT_MEMBER_GRANTS = ["camera.probe"]


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
