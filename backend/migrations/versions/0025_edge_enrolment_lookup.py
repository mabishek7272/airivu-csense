"""A narrow, privileged lookup for edge enrolment.

Enrolment has a chicken-and-egg problem. Row-level security scopes every query by
`app.tenant_id`, but a device presenting an enrolment token does not yet know which tenant
it belongs to - discovering that is the whole point of enrolling. The Tenant API connects
as `csense_api`, which is deliberately *not* a member of `csense_platform` and therefore
cannot bypass RLS. That is the correct arrangement and should not be relaxed: a
compromised Tenant API must not be able to read across tenants.

So the hole is made deliberately small instead. This function is `SECURITY DEFINER`, owned
by the schema owner, and does exactly one thing: given a token prefix, return the single
row needed to decide whether that token is valid and which tenant it belongs to. The
caller then sets `app.tenant_id` from the result and does everything else - the updates,
the credential issue - under normal row-level security.

Three properties make this safe to grant:

  It takes a prefix, not a tenant. There is no argument that lets a caller ask for
  another tenant's data; the only way to reach a row is to already hold something that
  hashes to it.

  It returns the hash, never a token. The comparison happens in the application, in
  constant time. Nothing here can be used to mint a credential.

  `search_path` is pinned. Without that, a caller able to create objects could shadow
  `edge_enrolment_tokens` with their own table and have this function read it with the
  owner's privileges - the classic SECURITY DEFINER escalation.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-27
"""
from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION edge_enrolment_lookup(p_prefix text)
        RETURNS TABLE (
            token_id uuid,
            device_id uuid,
            tenant_id uuid,
            token_hash text,
            expires_at timestamptz,
            redeemed_at timestamptz,
            device_name text,
            device_role text,
            serial_number text,
            device_status text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        -- Pinned so a caller cannot shadow the tables below with their own and have them
        -- read with the owner's privileges.
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id, t.device_id, t.tenant_id, t.token_hash, t.expires_at,
                   t.redeemed_at, d.name, d.role, d.serial_number, d.status
            FROM public.edge_enrolment_tokens t
            JOIN public.edge_devices d ON d.id = t.device_id
            WHERE t.token_prefix = p_prefix
              AND d.deleted_at IS NULL
            LIMIT 1;
        $$;
        """
    )

    # Nobody by default; then only the roles that actually enrol devices.
    op.execute("REVOKE ALL ON FUNCTION edge_enrolment_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION edge_enrolment_lookup(text) TO csense_api")
    op.execute(
        "GRANT EXECUTE ON FUNCTION edge_enrolment_lookup(text) TO csense_platform_api"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS edge_enrolment_lookup(text)")
