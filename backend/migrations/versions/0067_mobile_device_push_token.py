"""Adds push_token to mobile_devices - the natural home for "this phone's current Expo
push token," since this table already represents one phone/user/tenant. Nullable: a
device that hasn't opened Notifications settings yet (or denied the OS permission) simply
has none, and push dispatch skips it.

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mobile_devices", sa.Column("push_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mobile_devices", "push_token")
