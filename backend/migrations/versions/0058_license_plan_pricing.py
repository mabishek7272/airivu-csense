"""Adds list-price fields to license_plans - the schema this codebase genuinely has none
of today (no price/cost/currency column exists anywhere). Named "list price" throughout,
deliberately not "revenue"/"billed amount": no payment or invoicing system exists to know
what was actually charged or collected. This is what a reseller rollup's dollar total can
honestly mean right now - the sum of each child tenant's current plan's sticker price, not
real billing data.

price_cents (not a float) - the established convention for money-as-integer, avoiding
float rounding on an actual monetary value. Nullable: an existing plan (created before
this migration) has no price on record - NULL, not a fabricated 0, so a rollup can
distinguish "this plan costs $0" from "this plan's price was never set."

currency defaults to 'USD' via server_default so existing rows backfill to a concrete
value rather than NULL - unlike price, "what currency" isn't ambiguous for a platform
that has only ever operated in USD so far.

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("license_plans", sa.Column("price_cents", sa.BigInteger(), nullable=True))
    op.add_column(
        "license_plans",
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
    )


def downgrade() -> None:
    op.drop_column("license_plans", "currency")
    op.drop_column("license_plans", "price_cents")
