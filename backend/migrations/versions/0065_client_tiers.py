"""Client tiers: `mobile_devices` (which phones/tablets have signed in for a tenant) and the
two seeded plans, `standard` and `industry`.

**A tier is just a licence plan.** Two new entitlement codes carry the limits, read by
`csense_shared.licensing.limits.entitlement_limit`:

  - `mobile_device.count`      - phones/tablets that may be signed in (standard 2, industry 5)
  - `web_session.concurrent`   - browsers signed in at the same time (standard 1, industry 2)

A tenant whose licence has no such entitlement (every tenant that exists today) is
unlimited, so this changes nothing for anyone until a plan carrying the codes is assigned.
Camera limits are deliberately not in these plans: none were specified, so `camera.count`
stays a per-licence override. Plan prices are left null (no price on record, not $0).

**`mobile_devices`** counts distinct installs, not logins. A device is the app's own
install id (generated once on the phone), so signing in again on the same phone does not
use a second slot. `revoked_at` frees the slot; `session_id` is the device's current
refresh session so revoking a device also signs it out.

Revision ID: 0065
Revises: 0064
Create Date: 2026-09-24
"""
from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

_PLANS = [
    ("standard", "Standard", {"mobile_device.count": 2, "web_session.concurrent": 1}),
    ("industry", "Industry", {"mobile_device.count": 5, "web_session.concurrent": 2}),
]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    op.create_table(
        "mobile_devices",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("length(device_id) BETWEEN 8 AND 64", name="ck_mobile_device_id_len"),
    )
    op.create_index("uq_mobile_devices_tenant_device", "mobile_devices", ["tenant_id", "device_id"], unique=True)
    op.create_index("ix_mobile_devices_active", "mobile_devices", ["tenant_id"], postgresql_where=sa.text("revoked_at IS NULL"))

    op.execute("ALTER TABLE mobile_devices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mobile_devices FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY mobile_devices_tenant_isolation ON mobile_devices
        USING (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        WITH CHECK (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        """
    )
    for role_name in _app_roles():
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON mobile_devices TO "{role_name}"')

    bind = op.get_bind()
    for code, name, limits in _PLANS:
        entitlements = {
            key: {"value_type": "limit_numeric", "limit_numeric": value} for key, value in limits.items()
        }
        bind.execute(
            sa.text(
                "INSERT INTO license_plans (code, name, license_type, billing_period, default_entitlements) "
                "VALUES (:code, :name, 'subscription', 'yearly', CAST(:e AS jsonb)) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "name": name, "e": json.dumps(entitlements)},
        )


def downgrade() -> None:
    op.execute("DELETE FROM license_plans WHERE code IN ('standard', 'industry') AND id NOT IN (SELECT plan_id FROM licenses)")
    op.drop_table("mobile_devices")
