"""Adds retention policy for outbox_events and processed_events tables.

Outbox and processed_events tables grow indefinitely with traffic — with one event per
detection and thousands of detections per camera per day, this becomes an unbounded
growth problem. This migration documents the retention policy and provides cleanup
guidance. Production deployments should configure periodic cleanup via cron or similar.

**Retention policy (documented as schema comments):**
- outbox_events: keep 7 days; delete rows where processed_at < NOW() - INTERVAL '7 days'
- processed_events: keep 30 days; delete rows where created_at < NOW() - INTERVAL '30 days'

**Manual cleanup (for immediate use):**
```sql
DELETE FROM outbox_events WHERE processed_at < NOW() - INTERVAL '7 days';
DELETE FROM processed_events WHERE created_at < NOW() - INTERVAL '30 days';
```

**Automated cleanup (production):**
Configure a cron job or scheduled task to run the above SQL daily, or use pg_cron:
```sql
SELECT cron.schedule('cleanup-outbox', '0 2 * * *',
  'DELETE FROM outbox_events WHERE processed_at < NOW() - INTERVAL ''7 days''');
SELECT cron.schedule('cleanup-events', '0 2 * * *',
  'DELETE FROM processed_events WHERE created_at < NOW() - INTERVAL ''30 days''');
```

**Revisit trigger:** Monitor disk usage and query performance; if retention policy is too
aggressive (e.g., you need 30 days of audit history), adjust INTERVAL accordingly. If
tables grow faster than expected, may indicate a stuck worker that is not marking events
as processed.

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
    # Add retention policy comment to outbox_events
    op.execute(
        """
        COMMENT ON TABLE outbox_events IS
        'Outbox events awaiting delivery to subscribers. ' ||
        'Retention policy: delete rows where processed_at < NOW() - INTERVAL ''7 days'' ' ||
        'via daily cron job or manual cleanup. ' ||
        'See migration 0060 for full retention guidance.'
        """
    )

    # Add retention policy comment to processed_events
    op.execute(
        """
        COMMENT ON TABLE processed_events IS
        'Audit log of processed events (notifications sent, detections ingested, etc). ' ||
        'Retention policy: delete rows where created_at < NOW() - INTERVAL ''30 days'' ' ||
        'via daily cron job or manual cleanup. ' ||
        'See migration 0060 for full retention guidance.'
        """
    )

    # Ensure outbox_events has processed_at index for efficient cleanup queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_events_processed_at ON outbox_events(processed_at)
        WHERE processed_at IS NOT NULL
        """
    )

    # Ensure processed_events has created_at index for efficient cleanup queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_processed_events_created_at ON processed_events(created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_outbox_events_processed_at")
    op.execute("DROP INDEX IF EXISTS idx_processed_events_created_at")
    op.execute("COMMENT ON TABLE outbox_events IS NULL")
    op.execute("COMMENT ON TABLE processed_events IS NULL")
