"""Camera health use cases: offline, network, low FPS (CHECKLIST: "Camera health use
cases: offline, obstruction, glare/night-vision, low FPS, network").

**Scoped to what a connectivity probe can actually see, named explicitly rather than
silently narrowed.** `offline` (no answer at all) and `low FPS` (the stream's own
reported frame rate) were already directly measurable from the existing RTSP probe
(`camera_probe.py`) and its SDP-parsed metrics. `network` (answers, but slowly) is newly
measurable as of this migration's companion code change - `probe_stream` now times the
whole round trip. `obstruction` and `glare/night-vision` are **not** built this pass:
both need a decoded video frame to analyze (brightness/variance statistics), and
`camera_probe.py`'s own docstring already made a deliberate choice to speak RTSP
directly rather than shell out to ffmpeg - reversing that for the connectivity probe
itself would be the wrong place to add a frame-decode dependency. A real snapshot-based
health check is a legitimate, separate feature (this deployment's own H.265-only NVR and
IR night imagery mean any glare/obstruction thresholds would need real validation data
before being trusted, the same caution already recorded for detection thresholds against
that same hardware) - not a stub built into this table under time pressure.

Two additive, backward-compatible changes to `camera_health_events` (migration 0041):

- `status` gains a third value, `degraded` - mirrors `edge_health_events`' own
  ok/degraded/failed model (migration 0026) rather than camera health staying binary
  forever once it had more than "up" and "down" to say.
- `check_name` (free text, nullable, existing rows left NULL rather than backfilled -
  they predate the concept of naming which check produced them, and NULL says exactly
  that) - mirrors `edge_health_events.check_name` exactly: deliberately not a
  DB-constrained enum, since the check set is expected to grow (a future obstruction/
  glare check should not need its own migration to add a new check_name value, only to
  add the DB-level column/status value it didn't already have - which is why `degraded`
  is added now, ahead of actually needing it for a specific new check).

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE camera_health_status ADD VALUE IF NOT EXISTS 'degraded'")
    op.add_column("camera_health_events", sa.Column("check_name", sa.Text(), nullable=True))


def downgrade() -> None:
    # Postgres cannot drop a single enum value in place - downgrading the type itself
    # would require rebuilding it and every column that uses it. Since 'degraded' rows
    # are additive and no earlier migration relied on the enum being exactly two values,
    # leaving the enum widened on downgrade is the safe choice over a destructive rebuild.
    op.drop_column("camera_health_events", "check_name")
