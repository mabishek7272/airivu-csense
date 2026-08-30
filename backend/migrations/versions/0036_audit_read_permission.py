"""Permission for reading the audit trail - one tenant's own, or (platform) every
tenant's.

One shared code, `audit.read`, granted to both `tenant_owner` (customer audience) and
`platform_admin` (platform audience) - `TenantContext` and `PlatformContext` are already
separate types resolved from separate audience-scoped tokens (see
`csense_shared.security.tenant_context`), so a permission code meaning "read the audit
trail" can be shared across both without any risk of a tenant token picking up
platform-wide visibility - `require_permission` only ever checks the caller's own
already-resolved context.

`audit_events` itself has carried real, correctly-scoped rows (RLS-enabled since migration
0001, tenant-vs-platform policy since migration 0005) since every feature this session has
written through `record_audit_and_outbox` - this is the first read path against it.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-30
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("audit.read", "audit", "read", "elevated",
     "Read the audit trail - one's own tenant, or every tenant for a platform operator"),
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

    codes = [p[0] for p in _PERMISSIONS]
    _grant(bind, "tenant_owner", "customer", codes)
    _grant(bind, "platform_admin", "platform", codes)


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
