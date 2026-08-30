"""Proves the licensing/quota foundation works end-to-end for real: a platform admin
creates a plan and issues a license with a real `camera.count` limit, the tenant's own
license view shows it, camera creation is gated by the real row-locked reservation
(`csense_shared.licensing.quota.reserve_quota`) - one succeeds, the next is refused with a
real `402 quota_exceeded` - and a *second*, unlicensed tenant proves the "no license means
unlimited" safety property still holds (no regression on the camera-creation flow every
earlier feature this session already exercised).

    python scripts/e2e_licensing.py
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
PLATFORM_ADMIN_PASSWORD = "LicensingE2E!Platform123"
LIMITED_OWNER_PASSWORD = "LimitedOwner!Password123"
UNLIMITED_OWNER_PASSWORD = "UnlimitedOwner!Password456"


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


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors e2e_reseller.py's own bootstrap_platform_admin."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"licensing-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Licensing E2E') RETURNING id",
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


def make_site(owner_token: str, suffix: str) -> str:
    _, site = api("/api/v1/tenant/sites", {
        "name": f"Site {suffix}", "code": f"site-{suffix}",
    }, owner_token, expect=(201,))
    return site["id"]


def mfa_step_up(admin_token: str) -> None:
    """License issuance is now step-up-gated (TRD-SEC-010 - see scripts/e2e_mfa.py for
    the dedicated, thorough coverage of the MFA flow itself). This does the minimum real
    enroll -> confirm -> verify round trip so this script's own license issuance below
    isn't testing against a stale/decorative gate."""
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

    step(1, "Bootstrap a platform admin and create a plan with a real camera.count limit")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api(
        "/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD},
        host="console.localhost",
    )
    admin_token = admin_auth["access_token"]
    mfa_step_up(admin_token)  # license issuance is step-up-gated - see mfa_step_up's own docstring

    plan_code = f"licensing-e2e-{suffix}"
    status, _plan = api(
        "/api/v1/admin/license-plans",
        {
            "code": plan_code, "name": "E2E One-Camera Plan", "license_type": "standard",
            "billing_period": "yearly",
            "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 1}},
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "the platform admin can create a plan", failures)

    step(2, "Register the tenant that will be licensed, with no license yet")
    _, limited_auth = api("/api/v1/auth/register", {
        "organization_name": f"Licensing E2E Limited {suffix}",
        "email": f"limited-owner-{suffix}@northwind.example",
        "password": LIMITED_OWNER_PASSWORD,
        "display_name": "Limited Owner",
    })
    limited_token, limited_tenant_id = limited_auth["access_token"], limited_auth["tenant_id"]

    _, no_license_yet = api("/api/v1/tenant/license", token=limited_token, method="GET")
    check(no_license_yet is None, "before any license is issued, the tenant's own view is null, not an error", failures)

    step(3, "The platform admin issues the plan to this tenant")
    status, license_out = api(
        "/api/v1/admin/licenses",
        {"tenant_id": limited_tenant_id, "plan_code": plan_code},
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "the license issues successfully", failures)
    check(
        license_out["entitlements"].get("camera.count", {}).get("limit_numeric") == 1,
        "the issued license carries the real camera.count=1 entitlement", failures,
    )

    step(4, "The tenant's own license view now shows it, with real quota usage")
    _, own_license = api("/api/v1/tenant/license", token=limited_token, method="GET")
    check(own_license is not None, "the tenant can see its own license", failures)
    check(own_license["plan_code"] == plan_code, "it's the plan that was actually issued", failures)
    camera_quota = next((q for q in own_license["quota_usage"] if q["quota_code"] == "camera.count"), None)
    check(camera_quota is not None, "camera.count appears in quota_usage", failures)
    check(camera_quota is not None and camera_quota["limit_value"] == 1, "with the real limit of 1", failures)
    check(camera_quota is not None and camera_quota["consumed_value"] == 0, "and nothing consumed yet", failures)

    step(5, "The first camera is created - within quota")
    site_id = make_site(limited_token, suffix)
    status, _camera1 = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Camera One", "code": "cam-one",
    }, limited_token, expect=(201,))
    check(status == 201, "the first camera (within the limit of 1) is created", failures)

    step(6, "The second camera is refused - quota is real, not decorative")
    status, refusal = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Camera Two", "code": "cam-two",
    }, limited_token, expect=(402,))
    check(status == 402, "the second camera is refused with 402, not silently allowed", failures)
    check(refusal.get("code") == "quota_exceeded", "the refusal names itself clearly", failures)

    step(7, "The tenant's own quota view now reflects the real consumption")
    _, after_license = api("/api/v1/tenant/license", token=limited_token, method="GET")
    camera_quota_after = next(q for q in after_license["quota_usage"] if q["quota_code"] == "camera.count")
    check(camera_quota_after["consumed_value"] == 1, "consumed_value is 1, matching the one camera actually created", failures)

    step(8, "A second license for the same tenant is refused - one effective license at a time")
    status, _ = api(
        "/api/v1/admin/licenses",
        {"tenant_id": limited_tenant_id, "plan_code": plan_code},
        admin_token, host="console.localhost", expect=(409,),
    )
    check(status == 409, "issuing a second active license to the same tenant is refused (409)", failures)

    step(9, "A SEPARATE, unlicensed tenant proves 'no license = unlimited' - no regression")
    _, unlimited_auth = api("/api/v1/auth/register", {
        "organization_name": f"Licensing E2E Unlimited {suffix}",
        "email": f"unlimited-owner-{suffix}@northwind.example",
        "password": UNLIMITED_OWNER_PASSWORD,
        "display_name": "Unlimited Owner",
    })
    unlimited_token, unlimited_tenant_id = unlimited_auth["access_token"], unlimited_auth["tenant_id"]
    unlimited_site_id = make_site(unlimited_token, f"u{suffix}")
    for i in (1, 2):
        status, _ = api("/api/v1/tenant/cameras", {
            "site_id": unlimited_site_id, "name": f"Free Camera {i}", "code": f"free-cam-{i}",
        }, unlimited_token, expect=(201,))
        check(status == 201, f"unlicensed tenant camera #{i} succeeds - no quota row means unlimited", failures)

    step(10, "Clean up")
    psql(f"DELETE FROM tenants WHERE id IN ('{limited_tenant_id}', '{unlimited_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Licensing E2E Limited {suffix}', 'Licensing E2E Unlimited {suffix}')")
    psql(f"DELETE FROM license_plans WHERE code = '{plan_code}'")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'limited-owner-{suffix}@northwind.example', 'unlimited-owner-{suffix}@northwind.example')"
    )
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    print("    test tenants, plan, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - license issuance, entitlement materialization, and real quota enforcement all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
