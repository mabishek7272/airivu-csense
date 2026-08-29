"""Adds the two pipeline-registry permissions migration 0007 didn't already seed, and
grants the tenant-facing one to `tenant_owner`.

`pipeline.publish` and `pipeline.assign` were seeded back in migration 0007, ahead of the
tables that would make them do anything - the same forward-seeding this project already
does for schema (`detections.pipeline_version_id` sat as a bare column since migration
0012 for exactly the same reason). But 0007 only ever granted them to `platform_admin`,
and `pipeline.assign` is a *tenant* action - `POST /api/v1/tenant/cameras/{id}/
pipeline-assignments` runs under `TenantContext`, which only ever carries a tenant role's
grants, never a platform role's. Without this migration, no tenant user could ever hold
`pipeline.assign` at all, regardless of how the endpoint checks it.

`pipeline.read` and `pipeline.manage` are new: `read` for the two `GET /pipelines`-shaped
endpoints, `manage` for creating a pipeline family and a draft version - the model
registry's own `model.upload` covers the equivalent ground there.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-29
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

# code, resource, action, risk_level, description
_NEW_PERMISSIONS = [
    ("pipeline.read", "pipeline", "read", "standard", "View pipeline definitions and versions"),
    ("pipeline.manage", "pipeline", "manage", "elevated", "Create a pipeline and its draft versions"),
]

_PLATFORM_ADMIN_GRANTS = ["pipeline.read", "pipeline.manage"]


def _permission_id(bind, code: str):
    return bind.execute(sa.text("SELECT id FROM permissions WHERE code = :c"), {"c": code}).scalar_one()


def _role_id(bind, role_name: str, audience: str):
    return bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :n AND audience = :a"),
        {"n": role_name, "a": audience},
    ).scalar_one_or_none()


def _grant(bind, role_name: str, audience: str, codes: list[str]) -> None:
    role_id = _role_id(bind, role_name, audience)
    if role_id is None:
        return
    for code in codes:
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, effect) "
                "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
            ),
            {"r": role_id, "p": _permission_id(bind, code)},
        )


def upgrade() -> None:
    bind = op.get_bind()

    for code, resource, action, risk, description in _NEW_PERMISSIONS:
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

    _grant(bind, "platform_admin", "platform", _PLATFORM_ADMIN_GRANTS)
    # The tenant grant migration 0007 never added, for the endpoint this whole registry
    # exists to serve.
    _grant(bind, "tenant_owner", "customer", ["pipeline.assign"])


def downgrade() -> None:
    bind = op.get_bind()

    # Only the grant this migration itself added - `pipeline.assign` the permission, and
    # platform_admin's original grant of it, belong to migration 0007 and stay.
    tenant_owner_id = _role_id(bind, "tenant_owner", "customer")
    if tenant_owner_id is not None:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE role_id = :r AND permission_id = "
                "(SELECT id FROM permissions WHERE code = 'pipeline.assign')"
            ),
            {"r": tenant_owner_id},
        )

    codes = [p[0] for p in _NEW_PERMISSIONS]
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": codes},
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": codes})
