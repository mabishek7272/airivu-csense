"""A narrow, privileged read for fleet-wide VPN address allocation.

Provisioning a device's WireGuard tunnel needs to check two things against *every* other
device in the fleet, not just the calling tenant's own: that the address about to be
allocated is not already taken, and that the site LAN being declared does not overlap one
another tenant has already provisioned. Both checks exist specifically because the fleet
shares one WireGuard server - a second tenant's peer is exactly who a collision or an
overlapping route would affect.

Row-level security scopes every ordinary query by `app.tenant_id`, which is correct
everywhere else and would be wrong here: a check that can only see the calling tenant's
own devices cannot catch a collision with anyone else's, which is the one case this exists
to catch. So, as with `edge_enrolment_lookup` (migration 0025), the hole is made
deliberately small rather than widened generally:

  It returns only `vpn_address` and `lan_cidr` - no device id, no name, no tenant. A
  provisioning check needs to know an address or a range is *taken*; it has no legitimate
  reason to learn *by whom*, and returning that would leak one tenant's site topology to
  another's operator through what should be an addressing collision message.

  It is read-only and takes no argument - there is no parameter that lets a caller ask for
  a specific tenant's rows, because there is nothing to ask for.

  `search_path` is pinned, for the same reason as `edge_enrolment_lookup`: without it, a
  caller able to create objects could shadow `edge_devices` with their own table and have
  this function read it with the owner's privileges.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-29
"""
from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION edge_vpn_pool_snapshot()
        RETURNS TABLE (
            vpn_address inet,
            lan_cidr cidr
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT vpn_address, lan_cidr
            FROM public.edge_devices
            WHERE deleted_at IS NULL
              AND (vpn_address IS NOT NULL OR lan_cidr IS NOT NULL);
        $$;
        """
    )

    op.execute("REVOKE ALL ON FUNCTION edge_vpn_pool_snapshot() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION edge_vpn_pool_snapshot() TO csense_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS edge_vpn_pool_snapshot()")
