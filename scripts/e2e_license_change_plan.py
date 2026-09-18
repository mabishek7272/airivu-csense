"""End-to-end verification of admin-initiated license plan change - the real gap
issue_license's own refusal names ("Upgrade/downgrade/replace is a real, separate
flow... not built this pass"). Proves against the real running stack:

  1. Bootstrap a real platform admin, create two real plans with different
     camera.count limits (small=2, large=10).
  2. Issue the small plan to a real tenant, confirm a 3rd camera is really refused
     (402 quota_exceeded) - the existing, unmodified quota-enforcement path.
  3. Call the real POST /licenses/{id}/change-plan to move to the large plan.
  4. Confirm the OLD license row is really revoked (not just superseded in appearance) -
     a direct DB check - and the NEW license row is really active under the new plan.
  5. Confirm the real, previously-refused 3rd camera can now be created - proving the
     new quota_ledgers row is real, not just a status-string change.
  6. Confirm no manual DB write was used anywhere above except the initial platform-admin
     bootstrap (the same one every other licensing e2e script already needs, e.g.
     scripts/e2e_licensing.py's own bootstrap_platform_admin) - the plan change itself is
     100% through the real HTTP API.

Run from the repo root with the stack up:
    python scripts/e2e_license_change_plan.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password
from csense_shared.security.totp import totp_now

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "ChangePlanE2E!Platform123"
OWNER_PASSWORD = "ChangePlanOwner!Password456"


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
    # -q suppresses command-tag echoes (e.g. a bare "SET" line ahead of a SELECT's own
    # output when both are sent as one -c string) - without it, lines[0] below can grab
    # the echoed "SET" instead of the real first result row.
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-q", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors scripts/e2e_licensing.py's own bootstrap_platform_admin. Note this creates
    no organizations row (only users/platform_developers/platform_role_assignments) - the
    real Organization row in this script comes solely from the tenant owner's own
    /api/v1/auth/register call below, and only that one needs cleaning up."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"changeplan-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Change Plan E2E') RETURNING id",
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


def mfa_step_up(admin_token: str) -> None:
    _, enrolled = api("/api/v1/admin/auth/mfa/totp/enroll", token=admin_token, host="console.localhost", expect=(201,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/totp/confirm", {"code": code}, admin_token, host="console.localhost", expect=(200,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/verify", {"code": code}, admin_token, host="console.localhost", expect=(200,))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Change Plan E2E {suffix}"

    step(1, "Bootstrap a real platform admin, create small (limit=2) and large (limit=10) plans")
    admin_email, _ = bootstrap_platform_admin()
    _, admin_auth = api("/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD}, host="console.localhost")
    admin_token = admin_auth["access_token"]
    mfa_step_up(admin_token)

    api("/api/v1/admin/license-plans", {
        "code": f"small-{suffix}", "name": "Small", "license_type": "standard", "billing_period": "yearly",
        "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 2}},
    }, admin_token, host="console.localhost", expect=(201,))
    api("/api/v1/admin/license-plans", {
        "code": f"large-{suffix}", "name": "Large", "license_type": "standard", "billing_period": "yearly",
        "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 10}},
    }, admin_token, host="console.localhost", expect=(201,))

    step(2, "Register a tenant, issue the small plan, confirm the real 402 on a 3rd camera")
    owner_email = f"changeplan-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": organization_name,
        "email": owner_email, "password": OWNER_PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {"name": "Depot", "code": f"depot-{suffix}"}, owner_token, expect=(201,))
    site_id = site["id"]

    _, license_out = api("/api/v1/admin/licenses", {
        "tenant_id": tenant_id, "plan_code": f"small-{suffix}",
    }, admin_token, host="console.localhost", expect=(201,))
    old_license_id = license_out["id"]

    api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 1", "code": f"cam1-{suffix}"}, owner_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 2", "code": f"cam2-{suffix}"}, owner_token, expect=(201,))
    status, _ = api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 3", "code": f"cam3-{suffix}"}, owner_token, expect=(201, 402))
    check(status == 402, f"3rd camera is really refused under the small plan (got {status})", failures)

    step(3, "Call the real change-plan endpoint to move to the large plan")
    status, changed = api(
        f"/api/v1/admin/licenses/{old_license_id}/change-plan",
        {"new_plan_code": f"large-{suffix}"}, admin_token, host="console.localhost", expect=(200,),
    )
    check(status == 200, f"change-plan call succeeds (got {status})", failures)
    check(changed["plan_code"] == f"large-{suffix}", "response reports the new plan code", failures)
    new_license_id = changed["id"]

    step(4, "Confirm the real DB state: old license revoked, new license active under the new plan")
    old_status = psql(f"SELECT status FROM licenses WHERE id = '{old_license_id}'")
    check(old_status == "revoked", f"old license row is really revoked (got '{old_status}')", failures)
    new_status_and_plan = psql(
        f"SELECT l.status, p.code FROM licenses l JOIN license_plans p ON p.id = l.plan_id WHERE l.id = '{new_license_id}'"
    )
    check(
        new_status_and_plan == f"active|large-{suffix}",
        f"new license row is really active under the new plan (got '{new_status_and_plan}')", failures,
    )

    step(5, "Confirm the previously-refused 3rd camera can now really be created")
    status, _ = api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 3", "code": f"cam3-{suffix}"}, owner_token, expect=(201, 402))
    check(status == 201, f"3rd camera now succeeds under the new plan's real quota row (got {status})", failures)

    step(6, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    # The tenant owner's own /api/v1/auth/register call above creates a real
    # organizations row (organization_type=direct_customer, display_name=organization_name)
    # that deleting the tenant does NOT cascade-delete (tenants.organization_id has no ON
    # DELETE CASCADE toward organizations) - it must be removed explicitly, same gap found
    # and fixed in other e2e scripts this session (e.g. e2e_licensing.py, e2e_site_scoping.py).
    psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
    for email in (owner_email, admin_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant, organization, admin, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - license plan change verified for real: old license revoked, new license's own real quota enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
