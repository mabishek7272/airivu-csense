"""Demo app login audit: `demo_login_audit`, one row per visit to the mobile demo app's
login screen (name/place/company + a reference to the selfie taken).

This is NOT a tenant table and carries no `tenant_id` - the public demo app has no tenant
concept, and the login screen in front of it is an info-capture step, not a real access
gate (the credential it asks for is a shared, public one). The point of this table is
purely "who tried the demo, and from where" - an append-only visit log, not an
authorization record.

**Append-only, like `audit_events` (migration 0004).** `csense_demo_api` (a new role,
bootstrap_roles.py) gets INSERT + SELECT only, scoped to exactly this one table - not the
blanket schema-wide grant the two existing API roles get, since this service has no
business touching anything else in this database. No UPDATE/DELETE at the database level:
a visit record cannot be rewritten or erased once made.

**The selfie itself lives in MinIO** (`BUCKET_AUDIT_ARCHIVE`, csense_shared.storage.objects
- already provisioned for exactly this kind of record, unused until now), not in this
table - `selfie_object_key` is a reference, matching the "no photos in Postgres" convention
`face_identities` (0064) already established for a different kind of image.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-26
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy import sql

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

TABLE = "demo_login_audit"


def _demo_role() -> str:
    return os.environ.get("POSTGRES_DEMO_API_USER", "csense_demo_api")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("place", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("selfie_object_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    role = sql.quoted_name(_demo_role(), quote=True)
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT ON {TABLE} TO "{role}"')


def downgrade() -> None:
    role = sql.quoted_name(_demo_role(), quote=True)
    op.execute(f'REVOKE SELECT, INSERT ON {TABLE} FROM "{role}"')
    op.drop_table(TABLE)
