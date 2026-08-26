"""Converts evidence.detection_id into a real foreign key.

Migration 0011 typed this column as text because detections lived in MongoDB and could not
be referenced. Migration 0012 moved them into PostgreSQL, so the reference can now be
enforced rather than hoped for.

`ON DELETE SET NULL`, deliberately, not CASCADE. Evidence and detections have different
lifetimes: detections default to 30-day retention as high-volume telemetry, while evidence
defaults to 90 days because it is what an incident is actually *about*. Cascading would
mean the retention sweep silently destroyed incident imagery a month early. Setting null
keeps the snapshot and loses only the back-reference to the observation that produced it -
which by then has been swept anyway.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Any pre-existing values are MongoDB ids that no longer resolve to anything.
    op.execute("UPDATE evidence SET detection_id = NULL WHERE detection_id IS NOT NULL")
    op.alter_column(
        "evidence",
        "detection_id",
        type_=PG_UUID(as_uuid=True),
        postgresql_using="detection_id::uuid",
        existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_evidence_detection",
        "evidence",
        "detections",
        ["detection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_evidence_detection", "evidence", ["detection_id"],
        postgresql_where=sa.text("detection_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_detection", table_name="evidence")
    op.drop_constraint("fk_evidence_detection", "evidence", type_="foreignkey")
    op.alter_column("evidence", "detection_id", type_=sa.Text(), existing_nullable=True)
