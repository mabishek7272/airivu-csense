"""Notification policies, recipients, and per-attempt delivery records.

Implements docs/05_BACKEND_SCHEMA.md §10.1-10.4 and docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §18.

Three things this schema is built to guarantee:

1. **A delivery attempt is a row, not a log line.** `notification_deliveries` records every
   attempt with its provider, response and failure reason. When WhatsApp bans a number or
   Resend starts bouncing, that shows up as a rising failure rate on a queryable table
   rather than as silence — which is how alerting systems fail without anyone noticing.

2. **Policy versions are immutable.** An incident must be explainable months later: which
   rules were in force when it fired, and who was meant to be told. Editing a policy in
   place destroys that. `notification_policy_versions` is append-only and a notification
   records the exact version that produced it.

3. **Acknowledgement cancels escalation.** `notifications.cancelled_at` plus the
   escalation level on the incident is what stops the 3am phone call to the site manager
   after the guard already dealt with it (TRD §18).

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-26
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TENANT_OWNED = (
    "recipient_groups",
    "recipient_group_members",
    "notification_policies",
    "notification_policy_versions",
    "notifications",
    "notification_deliveries",
)

CHANNELS = ["in_app", "email", "whatsapp", "sms", "web_push", "webhook"]
NOTIFICATION_STATUS = ["pending", "scheduled", "sending", "sent", "failed", "cancelled"]
DELIVERY_STATUS = ["queued", "sending", "accepted", "delivered", "read", "failed", "abandoned"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
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


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (
        ("notification_channel", CHANNELS),
        ("notification_status", NOTIFICATION_STATUS),
        ("delivery_status", DELIVERY_STATUS),
    ):
        ENUM(*values, name=name, create_type=True).create(bind, checkfirst=True)

    def enum_col(name: str, values: list[str]) -> ENUM:
        return ENUM(*values, name=name, create_type=False)

    # --- recipient groups (SCH §10.1) -------------------------------------------
    op.create_table(
        "recipient_groups",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_recipient_group_name", "recipient_groups", ["tenant_id", "name"], unique=True)

    op.create_table(
        "recipient_group_members",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "recipient_group_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("recipient_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Either an existing user, or a bare contact (a site manager with no login).
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        # E.164, the only format WhatsApp and SMS providers accept unambiguously.
        sa.Column("phone_e164", sa.Text(), nullable=True),
        sa.Column("channels", JSONB(), nullable=False, server_default='["email"]'),
        # Quiet hours etc. A fire alarm ignores them; a housekeeping alert should not.
        sa.Column("active_schedule", JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "user_id IS NOT NULL OR email IS NOT NULL OR phone_e164 IS NOT NULL",
            name="ck_recipient_has_an_address",
        ),
        sa.CheckConstraint(
            "phone_e164 IS NULL OR phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_recipient_phone_is_e164",
        ),
    )
    op.create_index(
        "ix_recipient_members_group", "recipient_group_members", ["tenant_id", "recipient_group_id"]
    )

    # --- policies and immutable versions (SCH §10.2) ----------------------------
    op.create_table(
        "notification_policies",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("event_filter", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("active_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_notification_policy_name", "notification_policies", ["tenant_id", "name"], unique=True)

    op.create_table(
        "notification_policy_versions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "policy_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("notification_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        # Escalation ladder, channels per step, delays. Whole definition in one document
        # so a notification can always name exactly what produced it.
        sa.Column("definition_json", JSONB(), nullable=False),
        sa.Column("definition_sha256", sa.Text(), nullable=False),
        sa.Column("published_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("policy_id", "version_number", name="uq_policy_version_number"),
        sa.CheckConstraint("definition_sha256 ~ '^[0-9a-f]{64}$'", name="ck_policy_version_sha256"),
    )

    op.create_foreign_key(
        "fk_policy_active_version",
        "notification_policies",
        "notification_policy_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- notifications (SCH §10.3) ----------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", PG_UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True),
        # The domain event that triggered this, for idempotent consumption.
        sa.Column("event_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column(
            "policy_version_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("notification_policy_versions.id"),
            nullable=True,
        ),
        sa.Column("severity", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", enum_col("notification_status", NOTIFICATION_STATUS), nullable=False, server_default="pending"),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Set when an acknowledgement lands before this escalation step fires.
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_notifications_tenant_incident", "notifications", ["tenant_id", "incident_id"])
    # The scheduler's hot path: what is due to send right now.
    op.create_index(
        "ix_notifications_due",
        "notifications",
        ["scheduled_at"],
        postgresql_where=sa.text("status IN ('pending', 'scheduled')"),
    )
    # One notification per (incident, escalation level). An escalation ladder that fires
    # twice for the same step is how a 3am call happens after someone already responded.
    op.create_index(
        "uq_notification_incident_level",
        "notifications",
        ["tenant_id", "incident_id", "escalation_level"],
        unique=True,
        postgresql_where=sa.text("incident_id IS NOT NULL"),
    )

    # --- deliveries (SCH §10.4) --------------------------------------------------
    op.create_table(
        "notification_deliveries",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "notification_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", enum_col("notification_channel", CHANNELS), nullable=False),
        # The destination, redacted for display but needed to retry. Never a secret.
        sa.Column("recipient_ref", sa.Text(), nullable=False),
        sa.Column("recipient_name", sa.Text(), nullable=True),
        sa.Column("provider_code", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", enum_col("delivery_status", DELIVERY_STATUS), nullable=False, server_default="queued"),
        # The provider's own id, so a bounce webhook can be matched back to this row.
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        # Redacted: a provider error can echo the recipient address or an API key.
        sa.Column("failure_summary_redacted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_deliveries_notification", "notification_deliveries", ["tenant_id", "notification_id"]
    )
    op.create_index(
        "ix_deliveries_retryable",
        "notification_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('queued', 'sending')"),
    )
    # Matching a provider webhook back to its delivery.
    op.create_index(
        "ix_deliveries_provider_message",
        "notification_deliveries",
        ["provider_code", "provider_message_id"],
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    # One delivery per (notification, channel, recipient): a retry updates the row rather
    # than creating a second one, so "how many times was this person told" stays truthful.
    op.create_index(
        "uq_delivery_target",
        "notification_deliveries",
        ["notification_id", "channel", "recipient_ref"],
        unique=True,
    )

    for table in TENANT_OWNED:
        _enable_rls(table)

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in TENANT_OWNED:
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')
        # Published policy versions are immutable, like model and pipeline versions.
        op.execute(f'REVOKE UPDATE, DELETE ON notification_policy_versions FROM "{role}"')


def downgrade() -> None:
    for table in reversed(TENANT_OWNED):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_constraint("fk_policy_active_version", "notification_policies", type_="foreignkey")
    op.drop_table("notification_deliveries")
    op.drop_table("notifications")
    op.drop_table("notification_policy_versions")
    op.drop_table("notification_policies")
    op.drop_table("recipient_group_members")
    op.drop_table("recipient_groups")
    for name in ("delivery_status", "notification_status", "notification_channel"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
