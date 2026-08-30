"""Proves license grace/restriction/renewal work end-to-end for real (CHECKLIST:
"License grace/restriction + renewal flow"): a license issued already past its own
expiry lazily flips to `grace` on the tenant's very next read and does NOT restrict
resource creation; a license past its grace window lazily flips to `expired` and DOES
restrict it (a real 402, not a quota-shaped error); renewing restores `active` and
resource creation immediately, and extends the underlying quota ledger's own period so
quota tracking through the renewed term stays real rather than silently "unlimited"; a
revoked license refuses renewal outright.

No sleeps: the expiry clock is driven by issuing licenses with an `expires_at` already in
the past relative to real time, not by waiting for real time to pass.

    python scripts/e2e_license_lifecycle.py
"""
from __future__ import annotations

import datetime as dt
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
GRACE_OWNER_PASSWORD = "GraceOwner!Password123"
EXPIRED_OWNER_PASSWORD = "ExpiredOwner!Password456"


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
    """Mirrors e2e_licensing.py's own bootstrap_platform_admin."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"license-lifecycle-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'License Lifecycle E2E') RETURNING id",
            (email, email, hash_password(GRACE_OWNER_PASSWORD, settings)),
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
    _, site = api("/api/v1/tenant/sites", {"name": f"Site {suffix}", "code": f"site-{suffix}"}, owner_token, expect=(201,))
    return site["id"]


def mfa_step_up(admin_token: str) -> None:
    _, enrolled = api("/api/v1/admin/auth/mfa/totp/enroll", token=admin_token, host="console.localhost", expect=(201,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/totp/confirm", {"code": code}, admin_token, host="console.localhost", expect=(200,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/verify", {"code": code}, admin_token, host="console.localhost", expect=(200,))


def iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.UTC) + delta).isoformat()


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a platform admin and a one-camera plan")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api(
        "/api/v1/admin/auth/login", {"email": admin_email, "password": GRACE_OWNER_PASSWORD}, host="console.localhost",
    )
    admin_token = admin_auth["access_token"]
    mfa_step_up(admin_token)

    plan_code = f"lifecycle-e2e-{suffix}"
    api(
        "/api/v1/admin/license-plans",
        {
            "code": plan_code, "name": "E2E Lifecycle Plan", "license_type": "standard", "billing_period": "yearly",
            "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 5}},
        },
        admin_token, host="console.localhost", expect=(201,),
    )

    step(2, "A tenant is issued a license already past its own expiry, but still within grace")
    _, grace_auth = api("/api/v1/auth/register", {
        "organization_name": f"Lifecycle Grace {suffix}",
        "email": f"grace-owner-{suffix}@northwind.example",
        "password": GRACE_OWNER_PASSWORD, "display_name": "Grace Owner",
    })
    grace_token, grace_tenant_id = grace_auth["access_token"], grace_auth["tenant_id"]

    status, issued = api(
        "/api/v1/admin/licenses",
        {
            "tenant_id": grace_tenant_id, "plan_code": plan_code,
            "expires_at": iso(dt.timedelta(minutes=-5)), "grace_days": 1,
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    check(status == 201, "issuance with a past expires_at + grace_days succeeds", failures)
    check(issued["grace_ends_at"] is not None, "grace_ends_at was computed and stored", failures)

    step(3, "The tenant's very next read lazily flips it to grace, not silently active or gone")
    _, own = api("/api/v1/tenant/license", token=grace_token, method="GET")
    check(own is not None, "the license is still visible, not hidden once lapsed", failures)
    check(own["status"] == "grace", f"status is 'grace' (got {own.get('status')})", failures)

    step(4, "Grace does not restrict resource creation")
    site_id = make_site(grace_token, suffix)
    status, _ = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Grace Camera", "code": f"grace-cam-{suffix}",
    }, grace_token, expect=(201,))
    check(status == 201, "a tenant in grace can still create resources", failures)

    step(5, "A second tenant is issued a license already past both expiry and grace")
    _, expired_auth = api("/api/v1/auth/register", {
        "organization_name": f"Lifecycle Expired {suffix}",
        "email": f"expired-owner-{suffix}@northwind.example",
        "password": EXPIRED_OWNER_PASSWORD, "display_name": "Expired Owner",
    })
    expired_token, expired_tenant_id = expired_auth["access_token"], expired_auth["tenant_id"]

    status, expired_issued = api(
        "/api/v1/admin/licenses",
        {
            "tenant_id": expired_tenant_id, "plan_code": plan_code,
            "expires_at": iso(dt.timedelta(minutes=-5)), "grace_days": 0,
        },
        admin_token, host="console.localhost", expect=(201,),
    )
    expired_license_id = expired_issued["id"]

    step(6, "The tenant's own read shows it as expired, and resource creation is a real, distinct 402")
    _, own_expired = api("/api/v1/tenant/license", token=expired_token, method="GET")
    check(own_expired["status"] == "expired", f"status is 'expired' (got {own_expired.get('status')})", failures)

    expired_site_id = make_site(expired_token, f"x{suffix}")
    status, refusal = api("/api/v1/tenant/cameras", {
        "site_id": expired_site_id, "name": "Should Fail", "code": f"expired-cam-{suffix}",
    }, expired_token, expect=(402,))
    check(status == 402, "camera creation is refused for an expired license (402)", failures)
    check(refusal.get("code") == "license_restricted", "with the distinct license_restricted code, not quota_exceeded", failures)

    step(7, "Renewing restores active status and resource creation, for real, over real HTTP")
    new_expiry = iso(dt.timedelta(days=365))
    status, renewed = api(
        f"/api/v1/admin/licenses/{expired_license_id}/renew",
        {"expires_at": new_expiry, "grace_days": 14},
        admin_token, host="console.localhost", expect=(200,),
    )
    check(status == 200, "renewal succeeds", failures)
    check(renewed["status"] == "active", "the renewed license reports active immediately", failures)

    _, own_after_renew = api("/api/v1/tenant/license", token=expired_token, method="GET")
    check(own_after_renew["status"] == "active", "the tenant's own view also shows active now", failures)

    status, _ = api("/api/v1/tenant/cameras", {
        "site_id": expired_site_id, "name": "Now Allowed", "code": f"renewed-cam-{suffix}",
    }, expired_token, expect=(201,))
    check(status == 201, "camera creation now succeeds after renewal", failures)

    step(8, "The renewal really extended the quota ledger's own period, not just the license row")
    ledger_period_end = psql(
        f"SELECT period_end FROM quota_ledgers WHERE license_id = '{expired_license_id}' "
        "AND quota_code = 'camera.count'"
    )
    check(
        ledger_period_end.startswith(new_expiry[:10]),
        f"quota_ledgers.period_end now matches the new expiry (got {ledger_period_end!r})", failures,
    )

    step(9, "A revoked license refuses renewal outright")
    psql(f"UPDATE licenses SET status = 'revoked' WHERE id = '{expired_license_id}'")
    status, refusal2 = api(
        f"/api/v1/admin/licenses/{expired_license_id}/renew",
        {"expires_at": iso(dt.timedelta(days=30))},
        admin_token, host="console.localhost", expect=(409,),
    )
    check(status == 409, "renewing a revoked license is refused (409)", failures)
    check(refusal2.get("code") == "conflict", "with a clear conflict code", failures)

    step(10, "Clean up")
    psql(f"DELETE FROM tenants WHERE id IN ('{grace_tenant_id}', '{expired_tenant_id}')")
    psql(
        "DELETE FROM organizations WHERE display_name IN "
        f"('Lifecycle Grace {suffix}', 'Lifecycle Expired {suffix}')"
    )
    psql(f"DELETE FROM license_plans WHERE code = '{plan_code}'")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'grace-owner-{suffix}@northwind.example', 'expired-owner-{suffix}@northwind.example')"
    )
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    print("    test tenants, plan, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - license grace, restriction, and renewal all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
