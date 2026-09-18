"""Adds an index on evidence.camera_id, needed by the new dashboard live-camera-wall
endpoint (`GET /api/v1/tenant/dashboard/camera-thumbnails`) added alongside this
migration. That endpoint's query is "most recent evidence row per camera" — a
`DISTINCT ON (camera_id) ... ORDER BY camera_id, capture_time DESC` — which without this
index falls back to a sequential scan as the evidence table grows. `evidence.camera_id`
already existed as a plain foreign key (migration 0011) with no index of its own; the only
existing evidence index is `ix_evidence_tenant_incident` (tenant_id, incident_id), which
does not help a camera-scoped lookup.

Revision ID: 0061
Revises: 0060
Create Date: 2026-09-19
"""
from __future__ import annotations

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_camera_capture "
        "ON evidence (camera_id, capture_time DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evidence_camera_capture")
