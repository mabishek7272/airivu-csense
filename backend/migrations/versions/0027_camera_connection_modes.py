"""How each camera is reached, and which private addresses that makes legitimate.

The deployment guide documents four connectivity methods, and only one of them puts the
camera on a publicly routable address. The recommended production method - WireGuard -
puts it on 10.0.0.x, which the SSRF guard refuses outright. As built, the guard blocked
the recommended path. This is the correction.

The fix is not to allow private addresses. It is to allow *specific* private addresses
that a tenant has provisioned and we have recorded:

  `connection_mode` on the camera says how it is reached. `direct` is a public host or a
  DDNS name and needs no exception. `vpn` and `edge` are reached through a tunnel to a
  named edge device, and only then is a private address meaningful.

  `lan_cidr` on the edge device is the site network that tunnel routes. A camera at
  192.168.1.100 behind a peer is legitimate; the same address with no peer is not.

**The dangerous case is not the one that looks dangerous.** A tenant declaring
169.254.0.0/16 is obvious and easy to reject. The subtle one is a tenant declaring
172.18.0.0/16 - Docker's default bridge range. Traffic to that address would never reach
their tunnel, because the server's own local route wins; it would reach our Postgres. So
the check is not "is this a sensible private range" but "does this overlap anything this
host can already reach locally", and the reserved list is configuration, because it
depends on how the host is networked.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

# Mirrors the four methods in the RTSP deployment guide, plus `edge` for the case where
# the device does not merely tunnel but runs the models itself.
CONNECTION_MODES = (
    "direct",       # public IP or DDNS name; no exception needed
    "vpn",          # reached through a WireGuard peer at a recorded address
    "edge",         # an edge device fronts it - it may never be reachable from the cloud
    "cloud_relay",  # camera pushes to a relay; we pull from the relay's public address
)


def upgrade() -> None:
    # The site network the tunnel routes, e.g. 192.168.1.0/24. NULL means the only
    # address reachable through this device is its own VPN address.
    op.add_column("edge_devices", sa.Column("lan_cidr", postgresql.CIDR(), nullable=True))

    # `connection_mode` already exists with a default of 'edge_rtsp' from the original
    # schema, which was never one of the documented methods. Normalise it before the
    # constraints go on, so existing rows do not fail them.
    #
    # A row saying 'edge_rtsp' with no edge device is not an edge deployment - it is a
    # camera created before edge devices existed, which in practice means it is reached
    # directly. Mapping every such row to 'edge' would assert a tunnel that was never
    # provisioned, and the tunnel-needs-a-device constraint would then reject it. Which is
    # exactly what it did when this migration first ran, and the constraint was right.
    op.execute(
        """
        UPDATE cameras
        SET connection_mode = CASE
            WHEN edge_device_id IS NOT NULL THEN 'edge'
            ELSE 'direct'
        END
        WHERE connection_mode NOT IN (
        """
        + ", ".join(f"'{m}'" for m in CONNECTION_MODES)
        + ")"
    )
    # And any row that claims a tunnel mode without a device - none should exist, but a
    # constraint that fails on deploy is worse than one that finds nothing to fix.
    op.execute(
        "UPDATE cameras SET connection_mode = 'direct' "
        "WHERE connection_mode IN ('vpn', 'edge') AND edge_device_id IS NULL"
    )
    op.alter_column("cameras", "connection_mode", server_default="direct")
    op.create_check_constraint(
        "ck_cameras_connection_mode",
        "cameras",
        "connection_mode IN (" + ", ".join(f"'{m}'" for m in CONNECTION_MODES) + ")",
    )

    # A camera reached through a tunnel needs to say which one. Without the device there
    # is no allowlist to check its address against, so the private address would have to
    # be refused - and the failure would look like a guard bug rather than a missing link.
    op.create_check_constraint(
        "ck_cameras_tunnel_needs_device",
        "cameras",
        "connection_mode NOT IN ('vpn', 'edge') OR edge_device_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cameras_tunnel_needs_device", "cameras", type_="check")
    op.drop_constraint("ck_cameras_connection_mode", "cameras", type_="check")
    op.alter_column("cameras", "connection_mode", server_default="edge_rtsp")
    op.drop_column("edge_devices", "lan_cidr")
