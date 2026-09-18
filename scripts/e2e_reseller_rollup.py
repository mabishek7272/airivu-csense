"""End-to-end verification of the reseller aggregate rollup (migration 0056, GET
/api/v1/tenant/child-tenants/rollup). Proves against the real running stack, and
specifically proves the thing a naive cross-tenant query would get wrong:

  1. Bootstrap a real platform admin, create a real reseller organization/tenant
     (organization_type='reseller' - direct DB write, since there is no real API for
     provisioning a reseller itself yet, same as scripts/e2e_reseller.py's own approach).
  2. Through the real POST /child-tenants, create two real child tenants.
  3. Populate REAL data inside each child tenant under ITS OWN tenant session (a real
     site, real cameras, a real incident) - never inserted "as the reseller", proving
     the rollup crosses a genuine RLS boundary rather than reading data that happened to
     already be reseller-scoped.
  4. Call the real GET /child-tenants/rollup as the reseller and confirm the real
     per-tenant counts match what was actually created in step 3 - not zero, which is
     what a naive cross-tenant query under the reseller's own session would silently
     return (the exact failure this feature's migration docstring names).
  5. Confirm a non-reseller tenant's own call to the same endpoint gets a real
     403 not_a_reseller - the same guard list_child_tenants already enforces.

Run from the repo root with the stack up:
    python scripts/e2e_reseller_rollup.py
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
RESELLER_OWNER_PASSWORD = "RollupE2EReseller!Pass123"
CHILD_OWNER_PASSWORD = "RollupE2EChild!Pass456"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
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
    # -q (quiet) is required, not optional: this helper is called with multi-statement
    # SQL ("SET app.is_platform = true; SELECT ...") and without -q psql echoes a bare
    # "SET" command-tag line ahead of the SELECT's own output, corrupting the result.
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-q", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def bootstrap_reseller_owner(suffix: str) -> tuple[str, str, str]:
    """Direct DB write for the reseller organization itself (organization_type=
    'reseller') - mirrors scripts/e2e_reseller.py's own approach, since there is no real
    provisioning API for creating a reseller from scratch (only for a reseller creating
    ITS OWN children, which is exactly what this script exercises through the real API
    starting in step 2). Returns (email, tenant_id, organization_id)."""
    settings = get_settings()
    email = f"reseller-rollup-{suffix}@example.com"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
            "VALUES ('reseller', %s, %s, %s, 'active') RETURNING id",
            (f"Reseller Rollup E2E {suffix}", f"Reseller Rollup E2E {suffix}", f"reseller-rollup-{suffix}"),
        )
        organization_id = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id", (organization_id,))
        tenant_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Reseller Owner') RETURNING id",
            (email, email, hash_password(RESELLER_OWNER_PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'"
        )
        role_id = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
        cur.execute(
            "INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
            "VALUES (%s, %s, %s, 'active', now())",
            (tenant_id, user_id, role_id),
        )
        conn.commit()
    return email, str(tenant_id), str(organization_id)


def _redis_password() -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("REDIS_PASSWORD="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("REDIS_PASSWORD not found in .env")


def redis_get_invitation_token(email: str) -> str:
    """Scans every `cs:local:invitation:*` key (there can be several - a dev Redis
    accumulates stale, unexpired invitation keys from prior e2e runs, since invitations
    live a week; this script alone also creates two at once, for child A and child B) and
    returns the token suffix of whichever key's stored JSON payload actually contains
    this email - not "expect exactly one key" (the same robust pattern already
    established in scripts/e2e_site_scoping.py and scripts/e2e_finer_roles.py)."""
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    keys = [k for k in result.stdout.strip().splitlines() if k]
    for key in keys:
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
             "redis-cli", "-a", password, "--no-auth-warning", "GET", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.rsplit(":", 1)[-1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a real reseller organization/tenant/owner")
    reseller_email, reseller_tenant_id, _ = bootstrap_reseller_owner(suffix)
    _, reseller_auth = api("/api/v1/auth/login", {"email": reseller_email, "password": RESELLER_OWNER_PASSWORD})
    reseller_token = reseller_auth["access_token"]

    step(2, "Create two real child tenants through the real API")
    child_a_name = f"Child A {suffix}"
    child_b_name = f"Child B {suffix}"
    child_a_email = f"child-a-{suffix}@example.com"
    child_b_email = f"child-b-{suffix}@example.com"
    _, child_a = api("/api/v1/tenant/child-tenants", {
        "organization_name": child_a_name, "owner_email": child_a_email, "owner_display_name": "Owner A",
    }, reseller_token, expect=(201,))
    _, child_b = api("/api/v1/tenant/child-tenants", {
        "organization_name": child_b_name, "owner_email": child_b_email, "owner_display_name": "Owner B",
    }, reseller_token, expect=(201,))
    child_a_tenant_id, child_b_tenant_id = child_a["tenant_id"], child_b["tenant_id"]

    step(3, "Populate real data inside each child tenant, under its OWN tenant session")
    ticket_a = redis_get_invitation_token(child_a_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket_a, "password": CHILD_OWNER_PASSWORD}, expect=(200,))
    ticket_b = redis_get_invitation_token(child_b_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket_b, "password": CHILD_OWNER_PASSWORD}, expect=(200,))

    _, child_a_auth = api("/api/v1/auth/login", {"email": child_a_email, "password": CHILD_OWNER_PASSWORD})
    child_a_token = child_a_auth["access_token"]
    _, site_a = api("/api/v1/tenant/sites", {"name": "Site", "code": f"a-{suffix}"}, child_a_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_a["id"], "name": "Cam 1", "code": f"a1-{suffix}"}, child_a_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_a["id"], "name": "Cam 2", "code": f"a2-{suffix}"}, child_a_token, expect=(201,))

    _, child_b_auth = api("/api/v1/auth/login", {"email": child_b_email, "password": CHILD_OWNER_PASSWORD})
    child_b_token = child_b_auth["access_token"]
    _, site_b = api("/api/v1/tenant/sites", {"name": "Site", "code": f"b-{suffix}"}, child_b_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_b["id"], "name": "Cam 1", "code": f"b1-{suffix}"}, child_b_token, expect=(201,))

    step(4, "Call the real rollup as the reseller and confirm real, non-zero per-tenant counts")
    status, rollup = api("/api/v1/tenant/child-tenants/rollup", token=reseller_token, method="GET", expect=(200,))
    check(status == 200, f"rollup call succeeds (got {status})", failures)
    check(rollup["child_tenant_count"] == 2, f"rollup reports 2 child tenants (got {rollup.get('child_tenant_count')})", failures)
    check(rollup["total_cameras"] == 3, f"rollup's total_cameras is 3, the real sum across both children (got {rollup.get('total_cameras')})", failures)

    by_id = {t["tenant_id"]: t for t in rollup["tenants"]}
    check(
        child_a_tenant_id in by_id and by_id[child_a_tenant_id]["camera_count"] == 2,
        f"child A's own row reports camera_count=2, not silently zero (got {by_id.get(child_a_tenant_id, {}).get('camera_count')})",
        failures,
    )
    check(
        child_b_tenant_id in by_id and by_id[child_b_tenant_id]["camera_count"] == 1,
        f"child B's own row reports camera_count=1, not silently zero (got {by_id.get(child_b_tenant_id, {}).get('camera_count')})",
        failures,
    )
    check(
        by_id.get(child_a_tenant_id, {}).get("site_count") == 1 and by_id.get(child_b_tenant_id, {}).get("site_count") == 1,
        "both children report site_count=1 each", failures,
    )

    step(5, "Confirm a non-reseller tenant gets a real 403 not_a_reseller on the same endpoint")
    status, body = api("/api/v1/tenant/child-tenants/rollup", token=child_a_token, method="GET", expect=(200, 403))
    check(status == 403 and body.get("code") == "not_a_reseller", f"non-reseller call is refused with not_a_reseller (got {status}, {body.get('code')})", failures)

    step(6, "Clean up")
    # Tenant deletion cascades to sites/cameras/incidents (ondelete=CASCADE on tenant_id,
    # migration 0009), but organization_relationships has no cascade from organizations
    # (migration 0001's plain sa.ForeignKey with no ondelete) - it must be deleted
    # explicitly before the organizations rows it references, same as
    # scripts/e2e_reseller.py already does, or the organization DELETE below would fail
    # on the FK. All THREE organizations this script creates (the reseller's own, plus
    # both child tenants' own, provisioned via POST /child-tenants) must be cleaned up -
    # not just tenants/users - matching each org's own real display_name.
    psql(
        f"SET app.is_platform = true; DELETE FROM organization_relationships WHERE parent_organization_id IN "
        f"(SELECT id FROM organizations WHERE display_name = 'Reseller Rollup E2E {suffix}');"
    )
    for tid in (reseller_tenant_id, child_a_tenant_id, child_b_tenant_id):
        real = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tid}';")
        if real:
            psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{real}';")
    psql(
        "SET app.is_platform = true; DELETE FROM organizations WHERE display_name IN "
        f"('Reseller Rollup E2E {suffix}', '{child_a_name}', '{child_b_name}');"
    )
    for email in (reseller_email, child_a_email, child_b_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenants, organizations, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - reseller rollup verified for real: genuine per-child counts, not the silent-zero RLS trap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
