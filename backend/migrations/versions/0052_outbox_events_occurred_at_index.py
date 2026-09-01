"""A btree on outbox_events (occurred_at, id) - for two consumers, not one.

`outbox_events` shipped in migration 0001 with exactly one index,
`ix_outbox_unpublished` (`next_attempt_at WHERE published_at IS NULL`), and that index
serves *neither* of the two queries that actually read this table today:

  1. `tenant_api/app/api/realtime.py` - the per-tenant incident tail behind the live
     WebSocket. It runs
     `WHERE aggregate_type = 'incident' AND (occurred_at, id) > (:since_at, :since_id)
      ORDER BY occurred_at, id LIMIT :limit`
     once per poll, *per open browser tab*. This has been running against an unindexed
     table since it shipped; automatic webhook delivery only made it visible.
  2. `csense_shared/webhooks/dispatcher.py`'s `fan_out_due_outbox_events` - the new
     fan-out scan, `ORDER BY e.occurred_at LIMIT :limit` every
     `webhook_dispatch_poll_seconds` (default 10s), usually only to find nothing new.

Neither can use a partial index keyed on `next_attempt_at`, so both currently seq-scan
and then sort. The composite `(occurred_at, id)` is chosen over a bare `(occurred_at)`
because it exactly matches realtime.py's row-comparison cursor and its two-column sort -
the same tuple, in the same order - so the tail can walk the index and stop at LIMIT
instead of sorting the whole match set. The fan-out scan's single-column
`ORDER BY occurred_at` is a prefix of the same key and is served by it too.

The 24h cutoff `fan_out_due_outbox_events` applies bounds the *result set*, not the
*work*: without an index on `occurred_at`, Postgres has to read every row in the table to
evaluate that cutoff, so the cost tracks total table size rather than the one-day window.

Which is the real point, and worth stating plainly:

  **`outbox_events` has no retention or pruning anywhere.** Nothing in `ingest.py`,
  `realtime.py`, `models.py`, migration 0001, or any `scripts/e2e_*.py` ever deletes from
  it - it grows monotonically for the life of the deployment. At a few thousand events a
  day that is on the order of a million rows (~250 MB) a year, on a 16-core box with no
  GPU that `CLAUDE.md` already shows is CPU-bound.

  This index makes both queries cheap enough that the growth stops being urgent. It does
  **not** solve it. Whether outbox rows should be pruned after N days, archived to object
  storage, or kept forever as an event log is a genuine product/compliance decision that
  nobody has made yet - it is recorded here so it is revisited deliberately, not
  rediscovered the day the table becomes a problem.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-01
"""
from __future__ import annotations

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_outbox_events_occurred_at", "outbox_events", ["occurred_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_occurred_at", table_name="outbox_events")
