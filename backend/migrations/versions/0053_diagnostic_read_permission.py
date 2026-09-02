"""`diagnostic.read`: a customer-audience permission for reading one edge device's own
recent log tail and health snapshot.

This is the permission Task 2 of `docs/superpowers/plans/2026-09-02-diagnostic-access-and-
config-desired-state.md` gates its new `GET /devices/{id}/diagnostics` route with. Two
things are deliberate about its shape, mirroring migration 0044's own reasoning for
`support.read`/`support.revoke` (the "safe sharing" pattern migration 0036 established for
`audit.read`):

  **Granted to `tenant_owner` and `tenant_member`, not held back for support sessions.**
  Self-service diagnostics - a tenant admin looking at why their own device is degraded -
  is an ordinary customer capability with nothing platform-specific about it. The same
  permission code, checked by the same `require_permission(context, "diagnostic.read")`
  call, is what a `support_grants`-elevated `TenantContext` (`support_elevation.py`) also
  carries when a grant's `requested_scopes` includes it - one permission code, two ways to
  arrive at a `TenantContext` holding it, exactly the pattern `audit.read` and
  `support.read` already established. No platform-only variant is needed because the route
  itself never has to know which kind of context it was handed.

  **Deliberately absent from `DANGEROUS_SUPPORT_SCOPES`** (`admin_api/app/api/support.py`).
  That denylist exists to stop a support grant from minting something that outlives the
  grant itself - `membership.manage`, `api_client.manage`, and friends all create durable
  access. `diagnostic.read` is read-only and mints nothing: a platform developer who reads
  a device's logs during a grant walks away with knowledge, not a standing credential or
  role. Adding it to the denylist would defeat the entire point of building this on top of
  elevation in the first place.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-02
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("diagnostic.read", "diagnostic", "read", "elevated",
     "Read an edge device's recent logs and health snapshot for troubleshooting"),
]


def _grant(bind, role_name: str, audience: str, codes: list[str]) -> None:
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :n AND audience = :a"),
        {"n": role_name, "a": audience},
    ).scalar_one_or_none()
    if role_id is None:
        return
    for code in codes:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :c"), {"c": code}
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, effect) "
                "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
            ),
            {"r": role_id, "p": permission_id},
        )


def upgrade() -> None:
    bind = op.get_bind()

    for code, resource, action, risk, description in _PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
                "VALUES (:id, :code, :resource, :action, :risk, :desc) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(), "code": code, "resource": resource,
                "action": action, "risk": risk, "desc": description,
            },
        )

    _grant(bind, "tenant_owner", "customer", ["diagnostic.read"])
    _grant(bind, "tenant_member", "customer", ["diagnostic.read"])


def downgrade() -> None:
    bind = op.get_bind()
    codes = [p[0] for p in _PERMISSIONS]
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": codes},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": codes}
    )
