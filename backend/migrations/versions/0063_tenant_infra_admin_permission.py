"""Seed `tenant.infra.manage`: the one permission gating every new admin_api endpoint
that manages a tenant's operational setup on their behalf (Sites, Zones, Cameras, Edge
devices, Rules, Pipeline assignments).

No schema change here - every table these new endpoints touch already carries a
`pg_has_role(CURRENT_USER, 'csense_platform', 'MEMBER')` bypass branch on its RLS policy
(confirmed directly against the live schema this session: `cameras`, `sites`, `zones`,
`edge_devices`, `detection_rules`, `pipeline_assignments` all have it already), which is
exactly what `platform_session()`/`platform_db_session` already uses for every existing
admin_api file (`pipelines.py`, `models.py`, `organizations.py`, `branding.py`). This
migration only adds the permission gate - the new endpoints reuse that existing mechanism
rather than inventing a second one.

One bundled permission, not six - in practice an admin who can manage a tenant's cameras
also manages their sites/zones/edge/rules/pipeline-assignments; there is no real scenario
that needs finer-grained separation, and six near-identical permission rows would only add
seeding/grant overhead for a bundle that always moves together.

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-20
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same shape as 0056/0062's own new-permission seeding.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
            "VALUES (:id, 'tenant.infra.manage', 'tenant.infra', 'manage', 'elevated', "
            "'Manage a tenant''s sites, zones, cameras, edge devices, rules, and pipeline "
            "assignments on their behalf (platform)') "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"id": uuid.uuid4()},
    )
    role_id = bind.execute(
        sa.text(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' "
            "AND audience = 'platform'"
        )
    ).scalar_one()
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'tenant.infra.manage'")
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id, effect) "
            "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
        ),
        {"r": role_id, "p": permission_id},
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id = "
        "(SELECT id FROM permissions WHERE code = 'tenant.infra.manage')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'tenant.infra.manage'")
