"""Adds retention policy for outbox_events and processed_events tables.

Outbox and processed_events tables grow indefinitely with traffic — with one event per
detection and thousands of detections per camera per day, this becomes an unbounded
growth problem. This migration documents the retention policy and adds the indexes a
cleanup job needs. Production deployments should configure periodic cleanup via cron or
similar (the SQL is documented below, not run automatically by this migration).

**Real schema, verified against the live stack (this migration's first draft guessed
wrong column names for both tables and failed on `alembic upgrade head` until corrected
here - a reminder that a migration is not verified until it has actually run):**
- `outbox_events` has no `processed_at` column. The column that means "this event left
  the outbox" is `published_at` (nullable - null means still pending delivery).
- `processed_events` has no `created_at` column. Its timestamp column is `processed_at`.

**Retention policy:**
- outbox_events: keep 7 days *after publication* - delete only rows that have actually
  been published (`published_at IS NOT NULL`) and are older than that. An unpublished
  event is still work in progress and must never be deleted by a retention job; if one is
  old and still unpublished, that is a stuck-worker bug worth finding, not something to
  quietly delete evidence of.
- processed_events: keep 30 days from `processed_at`.

**Manual cleanup (for immediate use):**
```sql
DELETE FROM outbox_events WHERE published_at IS NOT NULL AND published_at < NOW() - INTERVAL '7 days';
DELETE FROM processed_events WHERE processed_at < NOW() - INTERVAL '30 days';
```

**Automated cleanup (production), e.g. via pg_cron:**
```sql
SELECT cron.schedule('cleanup-outbox', '0 2 * * *',
  'DELETE FROM outbox_events WHERE published_at IS NOT NULL AND published_at < NOW() - INTERVAL ''7 days''');
SELECT cron.schedule('cleanup-processed-events', '0 2 * * *',
  'DELETE FROM processed_events WHERE processed_at < NOW() - INTERVAL ''30 days''');
```

**Revisit trigger:** Monitor disk usage and query performance; if retention policy is too
aggressive (e.g., you need 30 days of audit history), adjust INTERVAL accordingly. A
growing count of old, still-unpublished outbox rows indicates a stuck consumer, not a
retention problem.

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-19
"""
from __future__ import annotations

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMENT ON TABLE takes a single string literal - it does not accept SQL expression
    # concatenation (`||`) the way a SELECT list does. Adjacent literals separated by a
    # newline are concatenated by Postgres itself, which is what's relied on below.
    op.execute(
        """
        COMMENT ON TABLE outbox_events IS
        'Outbox events awaiting or having completed delivery to subscribers. Retention '
        'policy: delete rows where published_at IS NOT NULL AND published_at < NOW() - '
        'INTERVAL ''7 days'' via daily cron job or manual cleanup. Never delete an '
        'unpublished row (published_at IS NULL) - that is unfinished work, not history. '
        'See migration 0060 for full retention guidance.'
        """
    )

    op.execute(
        """
        COMMENT ON TABLE processed_events IS
        'Idempotency ledger: which consumer has already processed which event. '
        'Retention policy: delete rows where processed_at < NOW() - INTERVAL ''30 days'' '
        'via daily cron job or manual cleanup. See migration 0060 for full retention '
        'guidance.'
        """
    )

    # Supports "published_at IS NOT NULL AND published_at < cutoff" cleanup queries.
    # ix_outbox_events_occurred_at (occurred_at, id) does not serve this - occurred_at is
    # when the event was created, not when it was published, and a row can sit unpublished
    # for a while before either timestamp lines up with the other.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_outbox_events_published_at
        ON outbox_events (published_at) WHERE published_at IS NOT NULL
        """
    )

    # processed_events' only index is its (consumer_name, event_id) primary key - no
    # existing index serves a retention scan over processed_at alone.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_processed_events_processed_at
        ON processed_events (processed_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outbox_events_published_at")
    op.execute("DROP INDEX IF EXISTS ix_processed_events_processed_at")
    op.execute("COMMENT ON TABLE outbox_events IS NULL")
    op.execute("COMMENT ON TABLE processed_events IS NULL")
