"""Sites, zones, cameras, incidents - the tenant-owned domain the pipeline writes into.

Implements docs/05_BACKEND_SCHEMA.md §7.1, §7.2, §7.7 and §9.2-9.4. Every table here is
tenant-owned and therefore carries `tenant_id` with FORCE ROW LEVEL SECURITY, using the
same group-membership-gated policy as migration 0005 - the platform bypass requires
membership of the `csense_platform` role, not merely a session flag an attacker could set.

Three decisions worth stating, because they are the ones that bite later:

1. **Incident numbers are per tenant and gap-free**, allocated from a locked counter row
   rather than a global sequence. Tenants see "Incident 42", not a shared global id that
   leaks how many incidents other tenants have.

2. **`correlation_key` plus a partial unique index is what actually prevents duplicate
   incidents.** A rule that fires on 25 consecutive frames must produce one incident, not
   25. Enforcing that in the database rather than in application logic means a retry, a
   concurrent worker, or a redelivered event cannot bypass it.

3. **Detections live in MongoDB, incidents in PostgreSQL** (SCH §2). `incident_detection_links`
   holds only the detection's id and capture time, since cross-store joins are not
   available - the link table is how an incident points at its evidence.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-26
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TENANT_OWNED_TABLES = (
    "sites",
    "zones",
    "cameras",
    "incidents",
    "incident_events",
    "incident_detection_links",
    "tenant_counters",
)

INCIDENT_STATUS = ["open", "acknowledged", "investigating", "escalated", "resolved", "dismissed"]
INCIDENT_SEVERITY = ["info", "low", "medium", "high", "critical"]
CAMERA_STATUS = ["provisioning", "ready", "degraded", "offline", "disabled", "retired"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def _enable_rls(table: str) -> None:
    """Same policy shape as migration 0005: tenant match, or platform group membership.

    `pg_has_role` is the important half - it means setting `app.is_platform` from a
    compromised tenant connection grants nothing, because that role is not a member.
    """
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
        ("incident_status", INCIDENT_STATUS),
        ("incident_severity", INCIDENT_SEVERITY),
        ("camera_status", CAMERA_STATUS),
    ):
        ENUM(*values, name=name, create_type=True).create(bind, checkfirst=True)

    def enum_col(name: str, values: list[str]) -> ENUM:
        return ENUM(*values, name=name, create_type=False)

    # --- sites (SCH §7.1) --------------------------------------------------------
    op.create_table(
        "sites",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("address_json", JSONB(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_sites_tenant_code", "sites", ["tenant_id", "code"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- zones: the ROI polygons rules are evaluated against (SCH §7.2) ----------
    op.create_table(
        "zones",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", PG_UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_zone_id", PG_UUID(as_uuid=True), sa.ForeignKey("zones.id"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("zone_type", sa.Text(), nullable=False, server_default="general"),
        # Normalised polygon: [[x,y], ...] with x,y in 0..1, matching the coordinate space
        # detections use, so a zone stays correct across stream resolutions.
        sa.Column("geometry_json", JSONB(), nullable=True),
        sa.Column("privacy_level", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_zones_tenant_site", "zones", ["tenant_id", "site_id"])

    # --- cameras (SCH §7.7, reduced to what the pipeline needs) ------------------
    op.create_table(
        "cameras",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", PG_UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("zone_id", PG_UUID(as_uuid=True), sa.ForeignKey("zones.id"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("connection_mode", sa.Text(), nullable=False, server_default="edge_rtsp"),
        # Endpoint and credentials live in encrypted_secrets, never here (SCH §1 rule 6).
        sa.Column("endpoint_secret_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("status", enum_col("camera_status", CAMERA_STATUS), nullable=False, server_default="provisioning"),
        sa.Column("capabilities", JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_frame_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_cameras_tenant_code", "cameras", ["tenant_id", "code"],
        unique=True, postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_cameras_tenant_site_status", "cameras", ["tenant_id", "site_id", "status"])

    # --- per-tenant counters, for gap-free incident numbers ----------------------
    op.create_table(
        "tenant_counters",
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("counter_name", sa.Text(), primary_key=True),
        sa.Column("current_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # --- incidents (SCH §9.2) ----------------------------------------------------
    op.create_table(
        "incidents",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", PG_UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("camera_id", PG_UUID(as_uuid=True), sa.ForeignKey("cameras.id"), nullable=False),
        sa.Column("incident_number", sa.BigInteger(), nullable=False),
        sa.Column("type_code", sa.Text(), nullable=False),
        sa.Column("severity", enum_col("incident_severity", INCIDENT_SEVERITY), nullable=False, server_default="medium"),
        sa.Column("status", enum_col("incident_status", INCIDENT_STATUS), nullable=False, server_default="open"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("correlation_key", sa.Text(), nullable=True),
        sa.Column("first_detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acknowledged_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_to_user_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_code", sa.Text(), nullable=True),
        sa.Column("resolution_summary", sa.Text(), nullable=True),
        sa.Column("detection_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_escalation_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "incident_number", name="uq_incident_number_per_tenant"),
    )
    op.create_index("ix_incidents_tenant_status_time", "incidents", ["tenant_id", "status", "first_detected_at"])
    op.create_index("ix_incidents_tenant_camera_time", "incidents", ["tenant_id", "camera_id", "first_detected_at"])

    # THE duplicate-suppression guarantee. Only one *live* incident may exist per
    # (tenant, camera, type, correlation_key); once resolved or dismissed the same key may
    # legitimately open a new one. Partial index, so closed incidents do not block reuse.
    op.create_index(
        "uq_incident_live_correlation",
        "incidents",
        ["tenant_id", "camera_id", "type_code", "correlation_key"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('resolved', 'dismissed') AND correlation_key IS NOT NULL"),
    )

    # --- incident_events: append-only history (SCH §9.4) -------------------------
    op.create_table(
        "incident_events",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", PG_UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("previous_status", sa.Text(), nullable=True),
        sa.Column("new_status", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_incident_events_incident", "incident_events", ["tenant_id", "incident_id", "occurred_at"])

    # --- incident <-> detection links (SCH §9.3) ---------------------------------
    op.create_table(
        "incident_detection_links",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", PG_UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        # Detections live in MongoDB, so this is an id reference, not a foreign key.
        sa.Column("detection_id", sa.Text(), nullable=False),
        sa.Column("capture_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("link_reason", sa.Text(), nullable=False, server_default="rule_match"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("incident_id", "detection_id", name="uq_incident_detection"),
    )
    op.create_index("ix_incident_links_tenant_incident", "incident_detection_links", ["tenant_id", "incident_id"])

    for table in TENANT_OWNED_TABLES:
        _enable_rls(table)

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        for table in TENANT_OWNED_TABLES:
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')
        # incident_events is append-only for the same reason audit_events is: a history
        # that can be edited is not a history (SCH §9.4).
        op.execute(f'REVOKE UPDATE, DELETE ON incident_events FROM "{role}"')


def downgrade() -> None:
    for table in reversed(TENANT_OWNED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("incident_detection_links")
    op.drop_table("incident_events")
    op.drop_table("incidents")
    op.drop_table("tenant_counters")
    op.drop_table("cameras")
    op.drop_table("zones")
    op.drop_table("sites")
    for name in ("camera_status", "incident_severity", "incident_status"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
