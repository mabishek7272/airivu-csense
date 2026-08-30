"""Proves the just-in-time support grant lifecycle works end-to-end for real: request,
peer approval (self-approval refused), the tenant's own "active support session" banner
data, the tenant's own right to revoke early, denial, lazy expiry, and cross-tenant
isolation.

    python scripts/e2e_support_grants.py
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

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "SupportGrantE2E!Platform123"
OWNER_PASSWORD = "SupportGrantE2E!Owner123"


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


def bootstrap_platform_admin(label: str) -> tuple[str, str, str]:
    """Mirrors e2e_mfa.py's own bootstrap_platform_admin. Returns (email, user_id, token)."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"support-e2e-{label}-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', %s) RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings), f"Support E2E {label}"),
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
    _, auth = api("/api/v1/admin/auth/login", {"email": email, "password": PLATFORM_ADMIN_PASSWORD}, host="console.localhost")
    return email, str(user_id), auth["access_token"]


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap two platform admins and register a tenant")
    admin_a_email, _admin_a_id, admin_a_token = bootstrap_platform_admin("a")
    admin_b_email, _admin_b_id, admin_b_token = bootstrap_platform_admin("b")

    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Support Grant E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD, "display_name": "Owner",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "A second, unrelated tenant exists to prove isolation later")
    _, other_auth = api("/api/v1/auth/register", {
        "organization_name": f"Support Grant E2E Other {suffix}",
        "email": f"other-owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD, "display_name": "Other Owner",
    })
    other_owner_token, other_tenant_id = other_auth["access_token"], other_auth["tenant_id"]

    step(3, "The tenant's own banner is empty before anything is requested")
    _, banner0 = api("/api/v1/tenant/support-grants?active_only=true", token=owner_token, method="GET")
    check(banner0 == [], "no active grant yet", failures)

    step(4, "Admin A requests a support grant against the tenant")
    status, requested = api(
        "/api/v1/admin/support-grants",
        {
            "tenant_id": tenant_id, "ticket_reference": f"TICKET-{suffix}",
            "purpose": "Investigating a customer-reported ingestion delay.",
            "requested_scopes": ["incident.read", "detection.read"], "ttl_hours": 8,
        },
        admin_a_token, host="console.localhost", expect=(201,),
    )
    check(status == 201 and requested["status"] == "requested", "grant created, awaiting approval", failures)
    grant_id = requested["id"]

    step(5, "Admin A cannot approve their own request")
    status, refusal = api(
        f"/api/v1/admin/support-grants/{grant_id}/approve", token=admin_a_token, host="console.localhost", expect=(403,),
    )
    check(status == 403, "self-approval is refused (403)", failures)
    check(refusal.get("code") == "self_approval_refused", "clear refusal code", failures)

    step(6, "Admin B, a real peer, approves it - and it becomes active")
    status, approved = api(
        f"/api/v1/admin/support-grants/{grant_id}/approve", token=admin_b_token, host="console.localhost", expect=(200,),
    )
    check(status == 200 and approved["status"] == "active", "approved by a peer -> active", failures)
    check(approved["starts_at"] is not None, "starts_at is set on approval", failures)

    step(7, "The tenant's own banner now shows it - this is what the customer sees")
    _, banner1 = api("/api/v1/tenant/support-grants?active_only=true", token=owner_token, method="GET")
    check(len(banner1) == 1 and banner1[0]["id"] == grant_id, "the active grant appears in the tenant's own view", failures)
    check(banner1[0]["developer_email"] == admin_a_email, "shows who requested it", failures)

    step(8, "A DIFFERENT tenant sees nothing - isolation holds")
    _, other_banner = api("/api/v1/tenant/support-grants?active_only=true", token=other_owner_token, method="GET")
    check(other_banner == [], "an unrelated tenant's banner is empty", failures)

    step(9, "The tenant ends the session early - their own real right, not just the platform's")
    status, revoked = api(
        f"/api/v1/tenant/support-grants/{grant_id}/revoke",
        {"reason": "Investigation complete, thanks."}, owner_token, expect=(200,),
    )
    check(status == 200 and revoked["status"] == "revoked", "the tenant's own revoke works", failures)

    _, banner2 = api("/api/v1/tenant/support-grants?active_only=true", token=owner_token, method="GET")
    check(banner2 == [], "the banner clears immediately after revoke", failures)

    step(10, "The platform side sees the same revoked state - one source of truth")
    _, admin_view = api(f"/api/v1/admin/support-grants?tenant_id={tenant_id}", token=admin_a_token, host="console.localhost", method="GET")
    matching = next((g for g in admin_view if g["id"] == grant_id), None)
    check(matching is not None and matching["status"] == "revoked", "the admin's own view agrees", failures)

    step(11, "A second request is denied instead of approved")
    _, requested2 = api(
        "/api/v1/admin/support-grants",
        {
            "tenant_id": tenant_id, "ticket_reference": f"TICKET-{suffix}-2",
            "purpose": "A second, unrelated investigation.",
            "requested_scopes": ["incident.read"], "ttl_hours": 4,
        },
        admin_a_token, host="console.localhost", expect=(201,),
    )
    status, denied = api(
        f"/api/v1/admin/support-grants/{requested2['id']}/deny",
        {"reason": "Not needed - resolved via the customer's own audit log instead."},
        admin_b_token, host="console.localhost", expect=(200,),
    )
    check(status == 200 and denied["status"] == "denied", "a real peer can deny instead of approve", failures)

    step(12, "Lazy expiry: an active grant past its expires_at flips to expired on the next read")
    _, requested3 = api(
        "/api/v1/admin/support-grants",
        {
            "tenant_id": tenant_id, "ticket_reference": f"TICKET-{suffix}-3",
            "purpose": "A third grant, to prove lazy expiry.",
            "requested_scopes": ["incident.read"], "ttl_hours": 1,
        },
        admin_a_token, host="console.localhost", expect=(201,),
    )
    api(f"/api/v1/admin/support-grants/{requested3['id']}/approve", token=admin_b_token, host="console.localhost", expect=(200,))
    psql(f"UPDATE support_grants SET expires_at = now() - interval '1 minute' WHERE id = '{requested3['id']}'")

    _, banner3 = api("/api/v1/tenant/support-grants?active_only=true", token=owner_token, method="GET")
    check(banner3 == [], "an expired grant no longer shows in the active banner", failures)
    _, admin_view3 = api(f"/api/v1/admin/support-grants?tenant_id={tenant_id}", token=admin_a_token, host="console.localhost", method="GET")
    expired_row = next((g for g in admin_view3 if g["id"] == requested3["id"]), None)
    check(expired_row is not None and expired_row["status"] == "expired", "the row's own status flipped to expired, not just hidden", failures)

    step(13, "Clean up")
    psql(f"DELETE FROM tenants WHERE id IN ('{tenant_id}', '{other_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Support Grant E2E {suffix}', 'Support Grant E2E Other {suffix}')")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'owner-{suffix}@northwind.example', 'other-owner-{suffix}@northwind.example', "
        f"'{admin_a_email}', '{admin_b_email}')"
    )
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - request, peer approval, the active banner, tenant revoke, denial, lazy expiry, and isolation all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
