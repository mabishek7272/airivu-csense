"""Adds the `annotated` evidence variant.

Evidence previously came in two forms: `original` (unmasked, restricted) and `masked`
(faces blurred, the default view). Neither draws the detection boundary, so an operator
opening an incident sees a photograph and has to guess which part of it triggered the
alert - in a busy scene that is genuinely ambiguous, and it is the first question anyone
asks.

The `annotated` variant is the masked image with the detection boxes and labels drawn on
it. It is derived from the masked variant, not the original, so the privacy default is
preserved: faces stay blurred and the box shows only *where* and *what*, never *who*.

It becomes the default thumbnail in listings. The raw coordinates are also returned by the
API so a UI can draw its own interactive overlay; the baked image exists for the places a
UI cannot reach - exports, email attachments, PDF reports.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block in older PostgreSQL;
    # 12+ allows it, and Alembic runs migrations transactionally, so use COMMIT-safe form.
    op.execute("COMMIT")
    op.execute("ALTER TYPE privacy_variant ADD VALUE IF NOT EXISTS 'annotated'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value. Removing it would mean recreating the
    # type and rewriting every dependent column - disproportionate for an additive change,
    # and the value is harmless if unused.
    pass
