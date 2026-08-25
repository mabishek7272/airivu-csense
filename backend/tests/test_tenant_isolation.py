"""Tenant isolation proof (docs/06_IMPLEMENTATION_PLAN.md IMP-G1: "Two test tenants prove
RLS/API/cache/object isolation").

Three connections, matching the three real database identities (SCH §15):

  owner    (TEST_POSTGRES_DSN)          superuser — used ONLY to create fixture rows,
                                        never to assert isolation.
  tenant   (TEST_POSTGRES_API_DSN)      what the Tenant API connects as.
  platform (TEST_POSTGRES_PLATFORM_DSN) what the Admin API connects as.

The central property under test is that the tenant role cannot reach another tenant's
rows *even if application code is fully compromised* — including by setting the
`app.is_platform` flag itself. That bypass additionally requires membership in the
`csense_platform` group role, which the tenant role does not have.

Requires a migrated database. Run `alembic upgrade head` (via the `migrate` service)
first; skipped automatically when the DSNs are absent so unit tests still run bare.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

REQUIRED_DSNS = ("TEST_POSTGRES_DSN", "TEST_POSTGRES_API_DSN", "TEST_POSTGRES_PLATFORM_DSN")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED_DSNS),
    reason=f"integration DSNs not set ({', '.join(REQUIRED_DSNS)}) — skipping",
)


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"], autocommit=True)


def _tenant_conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_POSTGRES_API_DSN"], autocommit=True)


def _platform_conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_POSTGRES_PLATFORM_DSN"], autocommit=True)


def _scope_to_tenant(cur, tenant_id) -> None:
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
    cur.execute("SELECT set_config('app.is_platform', 'false', false)")


@pytest.fixture()
def two_tenants():
    """Creates two tenants, each with one user and one membership, as the owner role."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' LIMIT 1")
        role_id = cur.fetchone()[0]

        created = []
        for label in ("a", "b"):
            suffix = uuid.uuid4().hex[:8]
            cur.execute(
                "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                "VALUES ('direct_customer', %s, %s, %s, 'active') RETURNING id",
                (f"Tenant {label} {suffix}", f"Tenant {label} {suffix}", f"tenant-{label}-{suffix}"),
            )
            org_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id",
                (org_id,),
            )
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO users (email_normalized, email_display, status, display_name) "
                "VALUES (%s, %s, 'active', 'Test User') RETURNING id",
                (f"user-{label}-{suffix}@example.com", f"user-{label}-{suffix}@example.com"),
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO memberships (tenant_id, user_id, role_id, status) VALUES (%s, %s, %s, 'active') "
                "RETURNING id",
                (tenant_id, user_id, role_id),
            )
            membership_id = cur.fetchone()[0]
            created.append(
                {"tenant_id": tenant_id, "user_id": user_id, "membership_id": membership_id, "role_id": role_id}
            )

        return created[0], created[1]


def test_application_roles_cannot_bypass_rls():
    """Guard against a vacuous suite: a superuser or BYPASSRLS role ignores every policy,
    so the assertions below would pass by accident."""
    for factory in (_tenant_conn, _platform_conn):
        with factory() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            role, is_super, bypasses_rls = cur.fetchone()
            assert not is_super, f"{role} is a superuser; isolation tests would be vacuous"
            assert not bypasses_rls, f"{role} has BYPASSRLS; isolation tests would be vacuous"


def test_tenant_cannot_read_another_tenants_rows(two_tenants):
    tenant_a, tenant_b = two_tenants
    with _tenant_conn() as conn, conn.cursor() as cur:
        _scope_to_tenant(cur, tenant_b["tenant_id"])

        cur.execute("SELECT count(*) FROM memberships WHERE tenant_id = %s", (tenant_a["tenant_id"],))
        assert cur.fetchone()[0] == 0, "tenant B must not see tenant A's memberships"

        # Its own row is still visible — the policy scopes, it doesn't just deny.
        cur.execute("SELECT count(*) FROM memberships WHERE tenant_id = %s", (tenant_b["tenant_id"],))
        assert cur.fetchone()[0] == 1


def test_tenant_cannot_write_into_another_tenant(two_tenants):
    tenant_a, tenant_b = two_tenants
    with _tenant_conn() as conn, conn.cursor() as cur:
        _scope_to_tenant(cur, tenant_b["tenant_id"])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO memberships (tenant_id, user_id, role_id, status) VALUES (%s, %s, %s, 'active')",
                (tenant_a["tenant_id"], tenant_a["user_id"], tenant_a["role_id"]),
            )


def test_tenant_role_cannot_escalate_by_setting_platform_flag(two_tenants):
    """The property that matters most: even a fully compromised Tenant API — one that can
    execute arbitrary SQL, including setting `app.is_platform` — still cannot read across
    tenants, because the policy also requires `csense_platform` group membership that this
    role does not have."""
    tenant_a, tenant_b = two_tenants
    with _tenant_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_b["tenant_id"]),))
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")  # attacker attempt

        cur.execute("SELECT count(*) FROM memberships WHERE tenant_id = %s", (tenant_a["tenant_id"],))
        assert cur.fetchone()[0] == 0, "setting app.is_platform must NOT grant cross-tenant read"

        cur.execute("SELECT pg_has_role(current_user, 'csense_platform', 'MEMBER')")
        assert cur.fetchone()[0] is False, "tenant role must not be in the platform group"


def test_platform_role_sees_across_tenants_only_when_it_opts_in(two_tenants):
    tenant_a, tenant_b = two_tenants
    with _platform_conn() as conn, conn.cursor() as cur:
        # Without the explicit opt-in, even the Admin API's role is scoped like anyone else.
        _scope_to_tenant(cur, tenant_b["tenant_id"])
        cur.execute("SELECT count(*) FROM memberships WHERE tenant_id = %s", (tenant_a["tenant_id"],))
        assert cur.fetchone()[0] == 0, "platform role must not read cross-tenant without opting in"

        # With it, cross-tenant reads are allowed — this is what platform_session() does.
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute("SELECT count(*) FROM memberships WHERE tenant_id = %s", (tenant_a["tenant_id"],))
        assert cur.fetchone()[0] == 1, "platform role should read cross-tenant after opting in"


def test_audit_events_are_append_only_for_application_roles(two_tenants):
    """SCH §11.1: no update/delete permission for application roles — enforced by GRANTs
    in the database, not merely by application convention."""
    tenant_a, _ = two_tenants
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_events (tenant_id, actor_type, action, outcome) "
            "VALUES (%s, 'user', 'test.action', 'success') RETURNING id",
            (tenant_a["tenant_id"],),
        )
        audit_id = cur.fetchone()[0]

    with _tenant_conn() as conn, conn.cursor() as cur:
        _scope_to_tenant(cur, tenant_a["tenant_id"])

        cur.execute("SELECT count(*) FROM audit_events WHERE id = %s", (audit_id,))
        assert cur.fetchone()[0] == 1, "tenant should read its own audit events"

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_events SET action = 'tampered' WHERE id = %s", (audit_id,))

    with _tenant_conn() as conn, conn.cursor() as cur:
        _scope_to_tenant(cur, tenant_a["tenant_id"])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM audit_events WHERE id = %s", (audit_id,))


def test_active_membership_lookup_returns_only_that_users_membership(two_tenants):
    """The SECURITY DEFINER login helper (migration 0005) must not become a general
    cross-tenant read primitive."""
    tenant_a, tenant_b = two_tenants
    with _tenant_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT membership_id, tenant_id FROM csense_active_membership_for_user(%s)",
            (tenant_a["user_id"],),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == tenant_a["membership_id"]
        assert rows[0][1] == tenant_a["tenant_id"]

        # A different user resolves to their own tenant, never a merged view.
        cur.execute(
            "SELECT tenant_id FROM csense_active_membership_for_user(%s)", (tenant_b["user_id"],)
        )
        assert cur.fetchall() == [(tenant_b["tenant_id"],)]
