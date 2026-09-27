"""Adds 'email_otp' to authenticator_type - the login-time MFA flow (backend/tenant_api/
app/api/auth.py's new /login/mfa-verify) uses this value in audit-event payloads for a
user who has no TOTP enrolled. The challenge itself lives in Redis
(csense_shared/security/login_mfa_tickets.py), not this enum - no user_authenticators row
is ever created with this type - so this migration is not load-bearing for login to work,
but keeps audit payloads consistent with a real enum value rather than a bare string with
no schema backing anywhere.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-27
"""
from __future__ import annotations

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres cannot add an enum value inside the same transaction a later statement in
    # this same migration run might use it in - commit first, matching this repo's own
    # migration 0017 precedent for the identical situation.
    op.execute("COMMIT")
    op.execute("ALTER TYPE authenticator_type ADD VALUE IF NOT EXISTS 'email_otp'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value - same precedent as migration 0017.
    pass
