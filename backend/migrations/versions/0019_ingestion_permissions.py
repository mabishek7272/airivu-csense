"""Permissions and a role for detection ingestion.

Adds `detection.ingest` and the rule-management permissions, plus an `edge_device` role
that holds ingestion and nothing else.

**Why a separate role rather than reusing tenant_owner.** The credential that ingests
detections lives on a device in a plant room, physically accessible to anyone who gets
into the building. It must not be able to read incidents, download unmasked evidence,
change notification policies or manage users. `edge_device` is deliberately a role with
exactly one grant, so a stolen edge credential can only submit detections - which is the
one thing an attacker gains nothing from.

**This is an interim arrangement, and it should not survive Phase 3.** The target is mutual
TLS with per-device certificates issued at enrolment, which is what the checklist calls
for. Until that exists, a bearer token scoped to a single permission is a smaller and more
honest step than inventing a second shared-secret scheme that would have to be unpicked
later. The interim is recorded here so it is not mistaken for the destination.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-27
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("detection.ingest", "detection", "ingest", "standard",
     "Submit detections from an edge device or runtime"),
    ("detection.read", "detection", "read", "standard",
     "View the detection feed for a tenant"),
    ("rule.read", "rule", "read", "standard",
     "View detection rules"),
    ("rule.manage", "rule", "manage", "elevated",
     "Create, edit and disable detection rules"),
]

_TENANT_OWNER_GRANTS = ["detection.read", "rule.read", "rule.manage"]
_TENANT_MEMBER_GRANTS = ["detection.read", "rule.read"]


def _permission_id(bind, code: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE code = :c"), {"c": code}
    ).scalar_one()


def _grant(bind, role_name: str, audience: str, codes: list[str]) -> None:
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :n AND audience = :a"),
        {"n": role_name, "a": audience},
    ).scalar_one_or_none()
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

    bind.execute(
        sa.text(
            "INSERT INTO roles (id, tenant_id, name, role_type, audience, description) "
            "VALUES (:id, NULL, 'edge_device', 'system', 'customer', :desc) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "desc": (
                "An edge device or AI runtime submitting detections. Holds detection.ingest "
                "and nothing else, so a credential taken from a device in a plant room "
                "cannot read incidents or evidence."
            ),
        },
    )

    _grant(bind, "edge_device", "customer", ["detection.ingest"])
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
        sa.text("DELETE FROM roles WHERE tenant_id IS NULL AND name = 'edge_device'")
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": codes}
    )
