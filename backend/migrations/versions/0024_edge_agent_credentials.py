"""The credential a device uses after it has enrolled.

An enrolment token is single-use and short-lived: it exists to get a box from "unboxed" to
"has an identity". What the device authenticates with afterwards - every heartbeat, every
detection it pushes - is a separate, long-lived credential issued at the end of enrolment.

Keeping them separate matters. The enrolment token travels: it is generated in the portal,
written onto a USB stick or read out over the phone to whoever is installing the device.
The agent credential never leaves the device. Reusing the enrolment token as the ongoing
credential would mean that whatever path it travelled by is a permanent way in.

Stored as a digest, not encrypted, because it only ever needs verifying - nothing in the
system has a reason to recover a device's credential, and a design that cannot recover it
cannot leak it either.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("edge_devices", sa.Column("agent_token_hash", sa.Text(), nullable=True))
    # Non-secret. Lets a log line or the console identify which credential is in use
    # without the credential being recoverable from either.
    op.add_column("edge_devices", sa.Column("agent_token_prefix", sa.Text(), nullable=True))
    op.add_column(
        "edge_devices",
        sa.Column("agent_token_issued_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Every authenticated device request looks the credential up by prefix and then
    # compares the digest in constant time, so the prefix must be indexed.
    op.create_index(
        "ix_edge_devices_agent_prefix",
        "edge_devices",
        ["agent_token_prefix"],
        postgresql_where=sa.text("agent_token_prefix IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_edge_devices_agent_prefix", table_name="edge_devices")
    op.drop_column("edge_devices", "agent_token_issued_at")
    op.drop_column("edge_devices", "agent_token_prefix")
    op.drop_column("edge_devices", "agent_token_hash")
