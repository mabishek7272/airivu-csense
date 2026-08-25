"""Adds model-registry permissions and grants them to platform_admin.

Permission codes follow `resource.action` per docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §7.3
(`model.promote` is named there explicitly).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# code, resource, action, risk_level, description
_PERMISSIONS = [
    ("model.read", "model", "read", "standard", "View the model registry and version metadata"),
    ("model.promote", "model", "promote", "elevated", "Change a model version's lifecycle state"),
    ("model.upload", "model", "upload", "elevated", "Register a new model version artifact"),
    ("pipeline.publish", "pipeline", "publish", "elevated", "Publish an immutable pipeline version"),
    ("pipeline.assign", "pipeline", "assign", "elevated", "Assign a pipeline version to cameras"),
]

_PLATFORM_ADMIN_GRANTS = ["model.read", "model.promote", "model.upload", "pipeline.publish", "pipeline.assign"]


def upgrade() -> None:
    bind = op.get_bind()

    for code, resource, action, risk_level, description in _PERMISSIONS:
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, resource, action, risk_level, description)
                VALUES (:id, :code, :resource, :action, :risk_level, :description)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4(),
                "code": code,
                "resource": resource,
                "action": action,
                "risk_level": risk_level,
                "description": description,
            },
        )

    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'")
    ).scalar_one_or_none()
    if role_id is None:
        return

    for code in _PLATFORM_ADMIN_GRANTS:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
        ).scalar_one()
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (role_id, permission_id, effect)
                VALUES (:role_id, :permission_id, 'allow')
                ON CONFLICT DO NOTHING
                """
            ),
            {"role_id": role_id, "permission_id": permission_id},
        )


def downgrade() -> None:
    codes = tuple(code for code, *_ in _PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN {codes})"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN {codes}")
