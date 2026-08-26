"""A `cancelled` state for deliveries, distinct from `abandoned`.

`delivery_status` had no way to say "this was never sent because it stopped being needed".
The only non-sent terminal state was `abandoned`, which means the opposite thing: we tried
repeatedly and gave up.

The distinction is not cosmetic. When a guard acknowledges an incident, every queued
escalation for it is stopped - and that is the system working exactly as intended. Filing
those under `abandoned` would make a delivery-success dashboard report a flood of failures
during precisely the incidents that were handled best, and would bury real provider
outages in the noise.

Postgres cannot add an enum value inside a transaction that later uses it, and Alembic
runs migrations in a transaction, so this uses the `IF NOT EXISTS` form and commits before
returning. There is no downgrade: Postgres offers no way to remove an enum value, and
rewriting the type would rewrite every delivery row for no operational gain.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older servers and
    # cannot be used in the same transaction that references the new value. Committing the
    # surrounding transaction first makes this safe on every supported version.
    op.execute("COMMIT")
    op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    # Deliberately not implemented. Postgres cannot drop an enum value, and recreating the
    # type would rewrite notification_deliveries entirely to undo a purely additive change.
    pass
