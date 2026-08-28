"""Edge devices: the gateways and inference boxes that sit at customer sites.

An edge device is whatever CSense runs on at the customer end - a Raspberry Pi carrying
only connectivity, a Jetson or DGX Spark running models locally, or an ordinary Windows or
Linux PC. They differ enormously in what they can do, and the schema is built around that
difference rather than pretending they are interchangeable.

**`role` is the field that matters most.** A Raspberry Pi 4 has no useful inference
capacity: it makes cameras reachable and nothing more, so the cloud pulls the stream and
runs the models. A Jetson or DGX Spark can run the models itself and send up detections
instead of video. Those are not the same product, and the difference decides where the CPU
cost lands - the 16-core cloud server can serve a handful of `gateway` sites or a great
many `inference` ones. Recording it per device is what makes capacity answerable.

**`vpn_address` is a /32, allocated by us.** The deployment guide's template hardcodes
10.0.0.2 for the client, which collides on the second site and caps the fleet at ~253
peers. Addresses are allocated from a managed pool and stored here, and the uniqueness
constraint is what makes a collision impossible rather than merely unlikely.

**Enrolment tokens are stored hashed, never in the clear.** The token is a bearer
credential that turns an unconfigured box into a trusted member of a tenant's network. A
database read must not yield a usable one, so the same reasoning applies as to user
passwords.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Hardware families we have deployed or intend to. `other` exists so an unfamiliar box can
# still be enrolled - refusing to register hardware because a list is out of date would
# block a deployment for no security benefit.
DEVICE_TYPES = (
    "raspberry_pi",
    "jetson_nano",
    "jetson_orin",
    "dgx_spark",
    "pc_linux",
    "pc_windows",
    "other",
)

# What the device does in the pipeline, which is a different question from what it is.
DEVICE_ROLES = (
    "gateway",    # connectivity only; the cloud pulls the stream and runs the models
    "inference",  # runs models locally and sends detections, not video
    "hybrid",     # both - reachable streams *and* local inference
)

# How the site is reached, per the RTSP deployment guide's four methods.
CONNECTIVITY_METHODS = ("wireguard", "cloud_relay", "port_forward", "direct")

DEVICE_STATUSES = (
    "pending",    # created in the portal, not yet enrolled by the device
    "enrolled",   # the device has claimed its token and has an identity
    "online",     # heartbeating
    "offline",    # was online, has stopped heartbeating
    "disabled",   # administratively stopped
    "retired",    # decommissioned; kept for history
)


def _check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


def upgrade() -> None:
    op.create_table(
        "edge_devices",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # A device can be registered before anyone decides where it goes.
        sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        # Reported by the device at enrolment and pinned thereafter, so a token stolen
        # from one box cannot be redeemed on another.
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("device_type", sa.Text(), nullable=False, server_default="other"),
        sa.Column("role", sa.Text(), nullable=False, server_default="gateway"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),

        # What the box actually has: cores, RAM, GPU model, VRAM, accelerators. Free-form
        # because the fields worth recording for a DGX are not the ones worth recording
        # for a Pi, and a rigid schema would lose whichever it was not designed for.
        sa.Column(
            "hardware", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("os_name", sa.Text(), nullable=True),
        sa.Column("os_version", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),

        # Chosen by the device's own decision engine, not by us - recorded so an operator
        # can see what it picked and why without opening a shell on the device.
        sa.Column("connectivity_method", sa.Text(), nullable=True),
        sa.Column("connectivity_reason", sa.Text(), nullable=True),
        # The /32 this device answers on inside the VPN. NULL until one is allocated.
        sa.Column("vpn_address", postgresql.INET(), nullable=True),
        sa.Column("wireguard_public_key", sa.Text(), nullable=True),

        # Capability limits the device reports, e.g. how many streams it can carry and
        # which model families it can run. Consulted before assigning cameras to it.
        sa.Column(
            "capabilities", postgresql.JSONB(), nullable=False, server_default="{}"
        ),

        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),

        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # A site can be removed while its device is being re-homed, so this detaches
        # rather than deleting the device record.
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="SET NULL"),
        _check("device_type", DEVICE_TYPES, "ck_edge_device_type"),
        _check("role", DEVICE_ROLES, "ck_edge_device_role"),
        _check("status", DEVICE_STATUSES, "ck_edge_device_status"),
        sa.CheckConstraint(
            "connectivity_method IS NULL OR connectivity_method IN "
            + "(" + ", ".join(f"'{v}'" for v in CONNECTIVITY_METHODS) + ")",
            name="ck_edge_connectivity_method",
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_edge_name"),
    )

    # A serial identifies one physical box; two records claiming the same one means a
    # cloned device or a duplicated registration, and both need to fail loudly.
    op.create_index(
        "uq_edge_devices_serial",
        "edge_devices",
        ["tenant_id", "serial_number"],
        unique=True,
        postgresql_where=sa.text("serial_number IS NOT NULL AND deleted_at IS NULL"),
    )
    # The VPN address must be unique across the whole fleet, not per tenant: every peer
    # shares one WireGuard interface, so two tenants allocated the same /32 would collide
    # in the routing table.
    op.create_index(
        "uq_edge_devices_vpn_address",
        "edge_devices",
        ["vpn_address"],
        unique=True,
        postgresql_where=sa.text("vpn_address IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_edge_devices_tenant_site",
        "edge_devices",
        ["tenant_id", "site_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.execute("ALTER TABLE edge_devices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE edge_devices FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON edge_devices
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
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON edge_devices TO csense_api")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON edge_devices TO csense_platform_api"
    )

    # --- Enrolment tokens --------------------------------------------------------------

    op.create_table(
        "edge_enrolment_tokens",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        # SHA-256 of the token. The plaintext is shown once, at creation, and never
        # stored - this is a bearer credential that makes an unconfigured box trusted, so
        # a database read must not yield a usable one.
        #
        # A plain digest rather than Argon2, and that is deliberate: Argon2 exists to make
        # guessing *low-entropy* secrets expensive. These tokens are 256 bits from a CSPRNG,
        # so there is nothing to guess, and a slow hash would only add latency to every
        # enrolment attempt - which is itself a denial-of-service lever.
        sa.Column("token_hash", sa.Text(), nullable=False),
        # A short, non-secret prefix so an operator can tell which token a log line refers
        # to without the token itself being recoverable.
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        # Which serial redeemed it, so a second attempt from different hardware is
        # visible rather than silently allowed.
        sa.Column("redeemed_by_serial", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["edge_devices.id"], ondelete="CASCADE"),
        sa.CheckConstraint("length(token_prefix) BETWEEN 4 AND 16", name="ck_token_prefix"),
    )

    # Redemption looks the token up by prefix, then verifies the hash - so the lookup must
    # be indexed while the comparison stays constant-time in the application.
    op.create_index(
        "ix_edge_tokens_prefix", "edge_enrolment_tokens", ["token_prefix"]
    )
    # At most one live token per device: reissuing invalidates the previous one, so a
    # token handed to a courier and then reissued cannot still be redeemed.
    op.create_index(
        "uq_edge_tokens_live_per_device",
        "edge_enrolment_tokens",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("redeemed_at IS NULL"),
    )

    op.execute("ALTER TABLE edge_enrolment_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE edge_enrolment_tokens FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON edge_enrolment_tokens
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
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON edge_enrolment_tokens TO csense_api"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON edge_enrolment_tokens "
        "TO csense_platform_api"
    )

    # --- Cameras belong to an edge device ----------------------------------------------

    op.add_column(
        "cameras", sa.Column("edge_device_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "cameras_edge_device_id_fkey", "cameras", "edge_devices",
        ["edge_device_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_cameras_edge_device", "cameras", ["edge_device_id"],
        postgresql_where=sa.text("edge_device_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_cameras_edge_device", table_name="cameras")
    op.drop_constraint("cameras_edge_device_id_fkey", "cameras", type_="foreignkey")
    op.drop_column("cameras", "edge_device_id")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON edge_enrolment_tokens")
    op.drop_index("uq_edge_tokens_live_per_device", table_name="edge_enrolment_tokens")
    op.drop_index("ix_edge_tokens_prefix", table_name="edge_enrolment_tokens")
    op.drop_table("edge_enrolment_tokens")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON edge_devices")
    op.drop_index("ix_edge_devices_tenant_site", table_name="edge_devices")
    op.drop_index("uq_edge_devices_vpn_address", table_name="edge_devices")
    op.drop_index("uq_edge_devices_serial", table_name="edge_devices")
    op.drop_table("edge_devices")
