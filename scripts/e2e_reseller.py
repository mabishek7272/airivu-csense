"""Proves the reseller + child-tenant foundation works end-to-end for real: a platform
admin provisions a reseller organization, its invited owner accepts and creates a child
tenant of their own, that child's invited owner accepts independently and lands in the
*child* tenant (not the reseller's), and a plain (non-reseller) tenant is refused when it
tries the same thing.

Two invitation tokens exist in this run but never at the same time - each is read from
Redis (`invitation_tickets.py`'s `GETDEL`) and consumed via accept-invitation before the
next one is created, so `read_invitation_token`'s "exactly one key" assumption (mirrored
from `e2e_memberships.py`) still holds at each point it's called.

    python scripts/e2e_reseller.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "ResellerE2E!Platform123"
RESELLER_OWNER_PASSWORD = "ResellerOwner!Password123"
CHILD_OWNER_PASSWORD = "ChildOwner!Password456"
PLAIN_OWNER_PASSWORD = "PlainOwner!Password789"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204), host="app.localhost"):
    headers = {"Content-Type": "application/json", "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _redis_password() -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("REDIS_PASSWORD="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("REDIS_PASSWORD not found in .env")


def read_invitation_token(email: str) -> str:
    """Scans every cs:local:invitation:* key and returns the token of whichever key's
    stored JSON payload actually contains this email - not "the only key found". A
    shared dev Redis commonly carries several stale, unexpired invitation keys left
    over from other e2e runs (invitations live a week), so assuming this script's own
    invite is the only one present is fragile in exactly the way this function used to
    be (matches the pattern already established in scripts/e2e_site_scoping.py and
    scripts/e2e_finer_roles.py, applied here after that exact fragility caused a real
    false failure once the shared Redis accumulated 12 stale keys)."""
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in [k for k in result.stdout.strip().splitlines() if k]:
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
             "redis-cli", "-a", password, "--no-auth-warning", "GET", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.rsplit(":", 1)[-1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors e2e_audit_log.py's own bootstrap_platform_admin."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"reseller-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Reseller E2E') RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
        conn.commit()
    return email, str(user_id)


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a platform admin and provision a reseller organization")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api(
        "/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD},
        host="console.localhost",
    )
    admin_token = admin_auth["access_token"]

    status, org = api(
        "/api/v1/admin/organizations",
        {
            "organization_name": f"Reseller E2E {suffix}",
            "organization_type": "reseller",
            "owner_email": f"reseller-owner-{suffix}@northwind.example",
            "owner_display_name": "Reseller Owner",
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "the platform admin can provision a reseller organization", failures)
    check(org["organization_type"] == "reseller", "the organization is recorded as a reseller", failures)
    check(org.get("invitation_link") is None, "the reseller owner's token is not echoed - it actually sent", failures)
    reseller_tenant_id = org["tenant_id"]

    step(2, "The reseller owner accepts their invitation and lands in the reseller tenant")
    token = read_invitation_token(f"reseller-owner-{suffix}@northwind.example")
    status, accepted = api("/api/v1/auth/accept-invitation", {
        "token": token, "password": RESELLER_OWNER_PASSWORD,
    }, expect=(200,))
    check(status == 200, "accept-invitation issues real tokens", failures)
    check(accepted["tenant_id"] == reseller_tenant_id, "the reseller owner lands in the reseller's own tenant", failures)
    reseller_owner_token = accepted["access_token"]

    step(3, "The reseller starts with no child tenants")
    _, empty_children = api(
        "/api/v1/tenant/child-tenants", token=reseller_owner_token, method="GET"
    )
    check(empty_children == [], "an empty list, not an error, before any child exists", failures)

    step(4, "The reseller creates a child tenant for one of its own customers")
    status, child = api(
        "/api/v1/tenant/child-tenants",
        {
            "organization_name": f"Reseller E2E Child {suffix}",
            "owner_email": f"child-owner-{suffix}@northwind.example",
            "owner_display_name": "Child Owner",
        },
        reseller_owner_token, expect=(201,),
    )
    check(status == 201, "the reseller can create a child tenant", failures)
    check(child.get("invitation_link") is None, "the child owner's token is not echoed - it actually sent", failures)
    child_tenant_id = child["tenant_id"]
    check(child_tenant_id != reseller_tenant_id, "the child tenant is a distinct tenant boundary (TRD §8.2)", failures)

    step(5, "The reseller's own child-tenant list now shows it")
    _, children = api("/api/v1/tenant/child-tenants", token=reseller_owner_token, method="GET")
    check(
        any(c["tenant_id"] == child_tenant_id for c in children),
        "the new child tenant appears in the reseller's own list", failures,
    )

    step(6, "The child's invited owner accepts independently and lands in the CHILD tenant, not the reseller's")
    token = read_invitation_token(f"child-owner-{suffix}@northwind.example")
    status, child_accepted = api("/api/v1/auth/accept-invitation", {
        "token": token, "password": CHILD_OWNER_PASSWORD,
    }, expect=(200,))
    check(status == 200, "the child owner's own invitation accepts independently", failures)
    check(
        child_accepted["tenant_id"] == child_tenant_id,
        "the child owner lands in the child tenant, not the reseller's", failures,
    )
    child_owner_token = child_accepted["access_token"]

    step(7, "The child tenant is NOT itself a reseller - it cannot create children of its own")
    status, refusal = api(
        "/api/v1/tenant/child-tenants",
        {"organization_name": "Should not work", "owner_email": "nobody@example.com", "owner_display_name": "Nobody"},
        child_owner_token, expect=(403,),
    )
    check(status == 403, "a reseller_customer organization is refused (403)", failures)
    check(refusal.get("code") == "not_a_reseller", "the refusal names itself clearly", failures)

    step(8, "A plain direct_customer tenant is refused the same way")
    _, plain_auth = api("/api/v1/auth/register", {
        "organization_name": f"Plain E2E {suffix}",
        "email": f"plain-owner-{suffix}@northwind.example",
        "password": PLAIN_OWNER_PASSWORD,
        "display_name": "Plain Owner",
    })
    plain_token, plain_tenant_id = plain_auth["access_token"], plain_auth["tenant_id"]
    status, refusal = api(
        "/api/v1/tenant/child-tenants",
        {"organization_name": "Should not work either", "owner_email": "nobody2@example.com", "owner_display_name": "Nobody"},
        plain_token, expect=(403,),
    )
    check(status == 403, "an ordinary direct_customer tenant is refused (403)", failures)
    check(refusal.get("code") == "not_a_reseller", "same clear refusal code", failures)

    step(9, "organization_relationships actually links parent to child")
    rel_count = psql(
        "SELECT count(*) FROM organization_relationships rel "
        "JOIN organizations parent ON parent.id = rel.parent_organization_id "
        "JOIN organizations child ON child.id = rel.child_organization_id "
        f"WHERE parent.display_name = 'Reseller E2E {suffix}' "
        f"AND child.display_name = 'Reseller E2E Child {suffix}' "
        "AND rel.relationship_type = 'reseller_customer' AND rel.status = 'active'"
    )
    check(rel_count == "1", "exactly one active reseller_customer relationship row exists", failures)

    step(10, "Clean up")
    psql(f"DELETE FROM organization_relationships WHERE parent_organization_id IN "
         f"(SELECT id FROM organizations WHERE display_name = 'Reseller E2E {suffix}')")
    psql(f"DELETE FROM tenants WHERE id IN ('{reseller_tenant_id}', '{child_tenant_id}', '{plain_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Reseller E2E {suffix}', 'Reseller E2E Child {suffix}', 'Plain E2E {suffix}')")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'reseller-owner-{suffix}@northwind.example', 'child-owner-{suffix}@northwind.example', "
        f"'plain-owner-{suffix}@northwind.example')"
    )
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    print("    test tenants, organizations, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - reseller provisioning, child-tenant creation, and the reseller-only guard all work for real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
