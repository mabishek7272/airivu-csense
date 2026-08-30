"""The Phase 2 vertical-slice test: reseller -> child tenant -> MFA enrollment -> empty
dashboard -> quota-exceeded rejection, chained through the real running stack end to end.
Every step here is a feature this session shipped and independently e2e-verified on its
own (scripts/e2e_reseller.py, e2e_tenant_mfa.py, e2e_mfa.py, e2e_licensing.py) - this
proves they compose into the one real customer journey the CHECKLIST names, not just that
each works in isolation.

    python scripts/e2e_vertical_slice.py
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
from csense_shared.security.totp import totp_now

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "VerticalSliceE2E!Platform123"
RESELLER_OWNER_PASSWORD = "ResellerOwner!Password123"
CHILD_OWNER_PASSWORD = "ChildOwner!Password456"


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


def read_invitation_token() -> str:
    """Mirrors e2e_memberships.py/e2e_reseller.py's own helper - see either's docstring."""
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    keys = [k for k in result.stdout.strip().splitlines() if k]
    if len(keys) != 1:
        raise RuntimeError(f"Expected exactly one invitation key, found {len(keys)}: {keys}")
    return keys[0].rsplit(":", 1)[-1]


def bootstrap_platform_admin() -> tuple[str, str]:
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"vslice-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Vertical Slice E2E') RETURNING id",
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


def admin_step_up(admin_token: str) -> None:
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

    step(1, "A platform admin provisions a reseller organization")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api("/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD}, host="console.localhost")
    admin_token = admin_auth["access_token"]

    status, org = api(
        "/api/v1/admin/organizations",
        {
            "organization_name": f"Vertical Slice Reseller {suffix}", "organization_type": "reseller",
            "owner_email": f"reseller-owner-{suffix}@northwind.example", "owner_display_name": "Reseller Owner",
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "reseller organization provisioned", failures)
    reseller_tenant_id = org["tenant_id"]

    step(2, "The reseller owner accepts their invitation")
    token = read_invitation_token()
    _, accepted = api("/api/v1/auth/accept-invitation", {"token": token, "password": RESELLER_OWNER_PASSWORD}, expect=(200,))
    reseller_owner_token = accepted["access_token"]
    check(accepted["tenant_id"] == reseller_tenant_id, "lands in the reseller's own tenant", failures)

    step(3, "reseller -> child tenant: the reseller creates a customer tenant")
    status, child = api(
        "/api/v1/tenant/child-tenants",
        {
            "organization_name": f"Vertical Slice Customer {suffix}",
            "owner_email": f"child-owner-{suffix}@northwind.example", "owner_display_name": "Child Owner",
        },
        reseller_owner_token, expect=(201,),
    )
    check(status == 201, "child tenant created", failures)
    child_tenant_id = child["tenant_id"]

    step(4, "The child tenant's own invited owner accepts independently")
    token = read_invitation_token()
    _, child_accepted = api("/api/v1/auth/accept-invitation", {"token": token, "password": CHILD_OWNER_PASSWORD}, expect=(200,))
    child_owner_token = child_accepted["access_token"]
    check(child_accepted["tenant_id"] == child_tenant_id, "lands in the child tenant, not the reseller's", failures)

    step(5, "empty dashboard: a brand-new tenant's dashboard is all real zeros, not an error")
    status, dashboard = api("/api/v1/tenant/dashboard", token=child_owner_token, method="GET", expect=(200,))
    check(status == 200, "dashboard loads", failures)
    check(
        dashboard == {
            "sites_count": 0, "cameras_count": 0, "incidents_open_count": 0,
            "incidents_total_count": 0, "team_members_count": 1,  # the owner itself
        },
        "every count is a real zero except the owner's own membership", failures,
    )

    step(6, "MFA enrollment: the child tenant's owner protects their own account")
    _, enrolled = api("/api/v1/tenant/auth/mfa/totp/enroll", token=child_owner_token, expect=(201,))
    code = totp_now(enrolled["secret"])
    status, confirmed = api("/api/v1/tenant/auth/mfa/totp/confirm", {"code": code}, child_owner_token, expect=(200,))
    check(status == 200 and len(confirmed["recovery_codes"]) == 10, "TOTP enrolled with real recovery codes", failures)
    _, mfa_status = api("/api/v1/tenant/auth/mfa/status", token=child_owner_token, method="GET")
    check(mfa_status["enrolled"] is True, "the child owner's own MFA status reflects it", failures)

    step(7, "The platform admin issues a one-camera license to the child tenant (step-up required)")
    admin_step_up(admin_token)
    plan_code = f"vslice-e2e-{suffix}"
    api(
        "/api/v1/admin/license-plans",
        {
            "code": plan_code, "name": "Vertical Slice One-Camera Plan", "license_type": "standard",
            "billing_period": "yearly",
            "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 1}},
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    status, _ = api(
        "/api/v1/admin/licenses", {"tenant_id": child_tenant_id, "plan_code": plan_code},
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "license issued to the child tenant", failures)

    step(8, "The child owner creates a site and one camera - within quota")
    _, site = api("/api/v1/tenant/sites", {"name": f"Site {suffix}", "code": f"site-{suffix}"}, child_owner_token, expect=(201,))
    status, _ = api(
        "/api/v1/tenant/cameras", {"site_id": site["id"], "name": "Camera One", "code": "cam-one"},
        child_owner_token, expect=(201,),
    )
    check(status == 201, "the first camera is created", failures)

    step(9, "quota-exceeded rejection: a second camera is refused for real")
    status, refusal = api(
        "/api/v1/tenant/cameras", {"site_id": site["id"], "name": "Camera Two", "code": "cam-two"},
        child_owner_token, expect=(402,),
    )
    check(status == 402, "the second camera is refused (402)", failures)
    check(refusal.get("code") == "quota_exceeded", "the refusal names itself clearly", failures)

    step(10, "The dashboard now reflects real usage, not the empty state from step 5")
    _, dashboard_after = api("/api/v1/tenant/dashboard", token=child_owner_token, method="GET")
    check(
        dashboard_after["sites_count"] == 1 and dashboard_after["cameras_count"] == 1,
        "sites_count and cameras_count both now read 1", failures,
    )

    step(11, "Clean up")
    psql(f"DELETE FROM organization_relationships WHERE parent_organization_id IN "
         f"(SELECT id FROM organizations WHERE display_name = 'Vertical Slice Reseller {suffix}')")
    psql(f"DELETE FROM tenants WHERE id IN ('{reseller_tenant_id}', '{child_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Vertical Slice Reseller {suffix}', 'Vertical Slice Customer {suffix}')")
    psql(f"DELETE FROM license_plans WHERE code = '{plan_code}'")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'reseller-owner-{suffix}@northwind.example', 'child-owner-{suffix}@northwind.example')"
    )
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    print("    test tenants, organizations, plan, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - reseller -> child tenant -> MFA enrollment -> empty dashboard -> quota-exceeded rejection, all real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
