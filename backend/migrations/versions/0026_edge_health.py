"""Edge device health: current state on the device, history only when something changes.

The ACDK agent reports at three cadences - infrastructure every 30 seconds, services every
2 minutes, quality every 10 minutes. Storing every report would mean roughly 2,880 rows per
device per day, which at a hundred devices is a quarter of a million rows a day recording,
almost entirely, that nothing happened.

So the split is: **current state lives on the device row** and is overwritten in place,
while `edge_health_events` records only transitions and failures. "Is this device healthy
right now" is answered by a single-row read, and "what happened to it last Tuesday" is
answered by a table that only contains things that actually happened.

That choice has a cost worth naming: a device that fails and recovers between two reports
leaves no trace. The agent is expected to report the failure it saw, not just its state at
poll time, which is why the event write is driven by what the agent says changed rather
than by the platform diffing snapshots.

Events expire. A health event is operational telemetry, not evidence - it is worth keeping
long enough to investigate an outage and no longer. A BRIN index on `expires_at` is the
right shape here because rows are written in time order and the cleanup scans a contiguous
range, so it costs a fraction of a btree's size.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# Mirrors the ACDK agent's three tiers.
HEALTH_LEVELS = (
    "infrastructure",  # tunnel up, address assigned, gateway reachable, DNS resolving
    "service",         # RTSP reachable, agent services running, forwarding rules present
    "quality",         # latency, frame rate, packet loss, throughput
)

EVENT_STATUSES = ("ok", "degraded", "failed", "recovered")


def upgrade() -> None:
    # The latest report from each tier, overwritten in place. Free-form because a Jetson
    # reporting GPU temperature and a Pi reporting tunnel handshake age have little in
    # common, and a rigid schema would lose whichever it was not designed for.
    op.add_column(
        "edge_devices",
        sa.Column("health", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    # Summarised so a listing can be filtered and sorted without unpacking JSONB.
    op.add_column(
        "edge_devices", sa.Column("health_status", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_edge_health_status", "edge_devices",
        "health_status IS NULL OR health_status IN ('ok', 'degraded', 'failed')",
    )

    op.create_table(
        "edge_health_events",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        # Which check, e.g. 'wireguard_handshake' or 'rtsp_reachable'. Free text because
        # the agent's check set grows through firmware updates, and a database enum would
        # have to be migrated before a new check could ever be reported.
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        # When the agent saw it, which is not when we received it - a device that was
        # offline reports its cached events on reconnect, and the difference between those
        # two timestamps is exactly what an outage investigation needs.
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["edge_devices.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "level IN (" + ", ".join(f"'{v}'" for v in HEALTH_LEVELS) + ")",
            name="ck_health_event_level",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in EVENT_STATUSES) + ")",
            name="ck_health_event_status",
        ),
        sa.CheckConstraint(
            "length(check_name) BETWEEN 1 AND 64", name="ck_health_event_check_name"
        ),
    )

    op.create_index(
        "ix_health_events_device_time",
        "edge_health_events",
        ["device_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_health_events_expiry",
        "edge_health_events",
        ["expires_at"],
        postgresql_using="brin",
    )

    op.execute("ALTER TABLE edge_health_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE edge_health_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON edge_health_events
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON edge_health_events TO csense_api")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON edge_health_events TO csense_platform_api"
    )

    # --- Agent authentication lookup ---------------------------------------------------
    #
    # Same shape, and the same reasoning, as edge_enrolment_lookup in 0025: a device
    # presenting its agent credential does not send a tenant, and must not be able to -
    # the credential is what establishes which tenant it belongs to. Row-level security
    # therefore cannot scope the lookup that resolves it.
    #
    # The exception stays one function that takes a token prefix and returns one row. It
    # returns the digest for the caller to compare in constant time, never anything that
    # could be replayed as a credential, and `search_path` is pinned so a caller cannot
    # shadow the table it reads.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION edge_agent_lookup(p_prefix text)
        RETURNS TABLE (
            device_id uuid,
            tenant_id uuid,
            token_hash text,
            device_status text,
            device_name text,
            device_role text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT d.id, d.tenant_id, d.agent_token_hash, d.status, d.name, d.role
            FROM public.edge_devices d
            WHERE d.agent_token_prefix = p_prefix
              AND d.agent_token_hash IS NOT NULL
              AND d.deleted_at IS NULL
            LIMIT 1;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION edge_agent_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION edge_agent_lookup(text) TO csense_api")
    op.execute("GRANT EXECUTE ON FUNCTION edge_agent_lookup(text) TO csense_platform_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS edge_agent_lookup(text)")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON edge_health_events")
    op.drop_index("ix_health_events_expiry", table_name="edge_health_events")
    op.drop_index("ix_health_events_device_time", table_name="edge_health_events")
    op.drop_table("edge_health_events")
    op.drop_constraint("ck_edge_health_status", "edge_devices", type_="check")
    op.drop_column("edge_devices", "health_status")
    op.drop_column("edge_devices", "health")
