"""Notification permissions.

`notification.manage` is platform-level, not tenant-level, and that split is deliberate:
one WhatsApp gateway number serves every tenant on the deployment, so unlinking it stops
alerting for all of them. That is an operator action, not a customer one.

Tenants get `notification.policy.manage` instead — control over who *their* alerts go to
and how they escalate, without touching the shared transport.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-26
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("notification.read", "notification", "read", "standard",
     "View notification providers, policies and delivery history"),
    ("notification.manage", "notification", "manage", "elevated",
     "Configure notification providers and WhatsApp instances (platform)"),
    ("notification.policy.manage", "notification.policy", "manage", "elevated",
     "Create and publish notification policies for a tenant"),
    ("notification.recipient.manage", "notification.recipient", "manage", "standard",
     "Manage recipient groups and their members"),
]

_PLATFORM_GRANTS = ["notification.read", "notification.manage"]
_TENANT_OWNER_GRANTS = [
    "notification.read",
    "notification.policy.manage",
    "notification.recipient.manage",
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

    _grant(bind, "platform_admin", "platform", _PLATFORM_GRANTS)
    _grant(bind, "tenant_owner", "customer", _TENANT_OWNER_GRANTS)
    _grant(bind, "tenant_member", "customer", ["notification.read"])


def downgrade() -> None:
    codes = tuple(code for code, *_ in _PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN {codes})"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN {codes}")
