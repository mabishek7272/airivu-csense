"""Permissions for the support-grant lifecycle (migration 0043).

`support.request`/`support.approve` are platform-only (a platform developer requesting,
and a *different* platform developer approving, privileged access into a tenant's own
account - self-approval is refused at the endpoint, not the permission layer, the same
way the zero-owners lockout guard is a business rule enforced in code rather than
something a permission grant alone could express).

`support.revoke` and `support.read` are each granted to **both** audiences - the same
safe sharing `audit.read` (migration 0036) already established: a permission code is
just a string two different `require_permission()` call sites check against their own
already-resolved, audience-typed context (`PlatformContext`/`TenantContext`), so nothing
about granting the same code to `platform_admin` and `tenant_owner` lets either reach the
other's context. `support.read` is what backs the "active support session" banner - a
tenant must be able to see a grant is open against its own account without needing a
platform login. `support.revoke` shared the same way is what gives the tenant its own
real right to end a support session early, not just the platform side.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-30
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("support.request", "support", "request", "elevated",
     "Request a time-boxed support grant for privileged access into a tenant's account"),
    ("support.approve", "support", "approve", "critical",
     "Approve or deny another platform developer's support grant request"),
    ("support.revoke", "support", "revoke", "elevated",
     "End an active support grant early, from either the platform or the affected tenant"),
    ("support.read", "support", "read", "standard",
     "View support grants - a tenant's own, or (platform) any tenant's"),
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

    _grant(bind, "platform_admin", "platform", ["support.request", "support.approve", "support.revoke", "support.read"])
    _grant(bind, "tenant_owner", "customer", ["support.revoke", "support.read"])
    _grant(bind, "tenant_member", "customer", ["support.read"])


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
