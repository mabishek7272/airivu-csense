"""Proves the audit query endpoints work for real, against real history - not a fresh
mock. audit_events has carried real rows since migration 0001; every feature built this
session writes through record_audit_and_outbox. This registers a tenant (which itself
writes a real "tenant.created" event) and confirms both the tenant-facing and the
platform-wide admin audit endpoints can find it, with the filters and pagination cursor
actually working.

    python scripts/e2e_audit_log.py
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
PASSWORD = "AuditLogE2E!Password123"


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
    """Mirrors e2e_model_registry.py's own bootstrap_admin."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"audit-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Audit E2E') RETURNING id",
            (email, email, hash_password(PASSWORD, settings)),
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

    step(1, "Register a tenant - itself writes a real audit event")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Audit Log E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Owner",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "The tenant's own audit endpoint sees it, newest first")
    _, page = api("/api/v1/tenant/audit-events", token=owner_token, method="GET")
    check(len(page["items"]) >= 1, "at least the registration event is visible", failures)
    tenant_created = next((e for e in page["items"] if e["action"] == "tenant.created"), None)
    check(tenant_created is not None, "the tenant.created event is present", failures)
    check(
        tenant_created is not None and tenant_created["outcome"] == "success",
        "it's recorded as a success", failures,
    )

    step(3, "Filtering by action narrows correctly")
    _, filtered = api(
        "/api/v1/tenant/audit-events?action=tenant.created", token=owner_token, method="GET"
    )
    check(
        len(filtered["items"]) >= 1 and all(e["action"] == "tenant.created" for e in filtered["items"]),
        "every returned row matches the action filter", failures,
    )

    step(4, "A cursor from a small page size actually pages")
    _, small_page = api(
        "/api/v1/tenant/audit-events?limit=1", token=owner_token, method="GET"
    )
    check(len(small_page["items"]) == 1, "limit=1 returns exactly one row", failures)
    if small_page.get("next_cursor"):
        _, next_page = api(
            f"/api/v1/tenant/audit-events?limit=1&cursor={small_page['next_cursor']}",
            token=owner_token, method="GET",
        )
        check(
            next_page["items"] and next_page["items"][0]["id"] != small_page["items"][0]["id"],
            "the next page returns a different row, not a repeat", failures,
        )

    step(5, "The platform-wide admin endpoint sees the same event, across tenants")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api(
        "/api/v1/admin/auth/login", {"email": admin_email, "password": PASSWORD},
        host="console.localhost",
    )
    admin_token = admin_auth["access_token"]

    _, admin_page = api(
        f"/api/v1/admin/audit-events?tenant_id={tenant_id}",
        token=admin_token, method="GET", host="console.localhost",
    )
    check(
        any(e["action"] == "tenant.created" for e in admin_page["items"]),
        "the admin view finds the same event when narrowed to this tenant", failures,
    )
    check(
        all(e["tenant_id"] == tenant_id for e in admin_page["items"]),
        "the tenant_id filter actually narrows - no other tenant's rows leak in", failures,
    )

    step(6, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Audit Log E2E {suffix}'")
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'owner-{suffix}@northwind.example'")
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - both audit endpoints query real, existing history correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
