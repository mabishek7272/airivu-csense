"""Platform role assignments.

docs/05_BACKEND_SCHEMA.md §5.9 defines `platform_developers` but does not specify how a
developer is granted a platform-audience role (memberships are tenant-scoped by
definition and cannot carry a NULL tenant_id). This table is a minimal, spec-consistent
extension — noted in CLARIFICATIONS.md — that plays the same role for platform operators
that `memberships` plays for tenant users, without touching the tenant-owned tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_role_assignments",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "platform_developer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("platform_developers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role_id", PG_UUID(as_uuid=True), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("platform_developer_id", "role_id", name="uq_platform_role_assignment"),
    )
    # Platform-global table — explicitly NOT subject to tenant RLS (SCH §1 rule 3):
    # it has no tenant_id column at all, so it is unreachable from any tenant-scoped
    # repository by construction, independent of RLS.


def downgrade() -> None:
    op.drop_table("platform_role_assignments")
