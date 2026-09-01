"""Proves an *active* support grant actually elevates a platform developer's request into
real read/write access on the named tenant's own data - the "authorization" half
CHECKLIST.md's support-grants entry deliberately deferred (Tasks 1-5 of
docs/superpowers/plans/2026-09-01-support-grant-authorization.md wired the mechanism;
this proves it end-to-end against the live stack, per this project's "verify for real, not
by inspection" convention in CLAUDE.md).

Covers: a real elevated read that actually returns the tenant's own seeded data, the same
call refused without the X-CSense-Support-Tenant-Id header, refused before the grant is
approved, refused for a different tenant's id, refused the instant the grant is revoked,
and the tenant's own audit trail carrying support_grant_id for a write taken under the
grant (audit.py's read models don't expose that column over HTTP, so that last assertion
confirms it directly in Postgres, keyed off the row the tenant's own audit-events call
already found - the tenant-facing verification the plan's own Task 6 asks for, plus the
DB-level proof that Task 5's threading actually reached a real request).

    python scripts/e2e_support_grant_authorization.py
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
PLATFORM_ADMIN_PASSWORD = "SupportGrantAuthE2E!Platform123"
OWNER_PASSWORD = "SupportGrantAuthE2E!Owner123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204), host="app.localhost", extra_headers=None):
    headers = {"Content-Type": "application/json", "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
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
    """Mirrors e2e_support_grants.py's own bootstrap_platform_admin. Returns (email, user_id, token)."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"support-auth-e2e-{label}-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', %s) RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings), f"Support Auth E2E {label}"),
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


def elevated_incidents_call(token: str, tenant_id_header: str | None, expect=(200, 401)):
    headers = {"X-CSense-Support-Tenant-Id": tenant_id_header} if tenant_id_header else {}
    return api(
        "/api/v1/tenant/incidents", token=token, method="GET",
        extra_headers=headers, expect=expect,
    )


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant with a real site/camera, and a second unrelated tenant for isolation")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Support Grant Auth E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD, "display_name": "Owner",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, other_auth = api("/api/v1/auth/register", {
        "organization_name": f"Support Grant Auth E2E Other {suffix}",
        "email": f"other-owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD, "display_name": "Other Owner",
    })
    other_tenant_id = other_auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
    }, owner_token, expect=(201,))
    site_id = site["id"]

    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Gate Camera", "code": f"gate-{suffix}",
    }, owner_token, expect=(201,))
    camera_id = camera["id"]

    step(2, "Seed one real incident directly (no public creation endpoint - see e2e_exports.py's own docstring)")
    incident_id = psql(
        "INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, "
        "type_code, severity, status, title, first_detected_at, last_detected_at) VALUES ("
        f"'{tenant_id}', '{site_id}', '{camera_id}', 1, 'zone.intrusion', "
        "'high', 'open', 'Intrusion #1', now(), now()) RETURNING id"
    )
    check(bool(incident_id), "one incident seeded", failures)

    step(3, "Bootstrap two platform developers (peer-approval needs two, per the existing self-approval refusal)")
    dev_a_email, _dev_a_id, dev_a_token = bootstrap_platform_admin("a")
    _dev_b_email, _dev_b_id, dev_b_token = bootstrap_platform_admin("b")

    step(4, "Developer A requests a grant against the tenant (incident.read + incident.acknowledge, so both the elevated read and the elevated write proofs below use one real grant)")
    status, requested = api(
        "/api/v1/admin/support-grants",
        {
            "tenant_id": tenant_id, "ticket_reference": f"TICKET-{suffix}",
            "purpose": "Investigating a customer-reported incident-visibility issue.",
            "requested_scopes": ["incident.read", "incident.acknowledge"], "ttl_hours": 8,
        },
        dev_a_token, host="console.localhost", expect=(201,),
    )
    check(status == 201 and requested["status"] == "requested", "grant created, awaiting approval", failures)
    grant_id = requested["id"]

    step(5, "Before approval: the elevated call is refused (401) - an unapproved grant does not elevate")
    status, _ = elevated_incidents_call(dev_a_token, tenant_id, expect=(401,))
    check(status == 401, "no active grant yet -> 401", failures)

    step(6, "Developer B, a real peer, approves it - and it becomes active")
    status, approved = api(
        f"/api/v1/admin/support-grants/{grant_id}/approve", token=dev_b_token, host="console.localhost", expect=(200,),
    )
    check(status == 200 and approved["status"] == "active", "approved by a peer -> active", failures)

    step(7, "Developer A's platform token + X-CSense-Support-Tenant-Id now reads the tenant's real incident data")
    status, page = elevated_incidents_call(dev_a_token, tenant_id, expect=(200,))
    check(status == 200, "elevated call succeeds (200)", failures)
    returned_ids = {item["id"] for item in page.get("items", [])}
    check(incident_id in returned_ids, "the seeded incident is actually present in the response body", failures)

    step(8, "The identical call without X-CSense-Support-Tenant-Id is refused (401)")
    status, _ = elevated_incidents_call(dev_a_token, None, expect=(401,))
    check(status == 401, "missing header -> 401", failures)

    step(9, "The identical call naming a different, unrelated tenant is refused (401) - the grant only names one tenant")
    status, _ = elevated_incidents_call(dev_a_token, other_tenant_id, expect=(401,))
    check(status == 401, "wrong tenant id -> 401, even for the same developer/grant", failures)

    step(10, "Developer A performs a real elevated WRITE (acknowledge) under the active grant")
    status, ack = api(
        f"/api/v1/tenant/incidents/{incident_id}/acknowledge", {"reason": "Support investigation."},
        token=dev_a_token, extra_headers={"X-CSense-Support-Tenant-Id": tenant_id}, expect=(200,),
    )
    check(status == 200 and ack.get("status") == "acknowledged", "the elevated write succeeds", failures)

    step(11, "The tenant's own audit trail (their own real customer login) shows the action")
    _, audit_page = api(
        "/api/v1/tenant/audit-events?action=incident.acknowledged", token=owner_token, method="GET",
    )
    audit_row = next((e for e in audit_page.get("items", []) if e["target_id"] == incident_id), None)
    check(audit_row is not None, "the tenant's own audit trail contains this action", failures)

    step(12, "That audit row's own support_grant_id column equals this grant's id - Task 5's threading reached a real request")
    if audit_row is not None:
        actual_grant_id = psql(f"SELECT support_grant_id FROM audit_events WHERE id = '{audit_row['id']}'")
        check(actual_grant_id == grant_id, f"audit row carries support_grant_id={grant_id!r} (got {actual_grant_id!r})", failures)
    else:
        check(False, "audit row's support_grant_id (skipped - no audit row found)", failures)

    step(13, "Developer B revokes the grant")
    status, revoked = api(
        f"/api/v1/admin/support-grants/{grant_id}/revoke",
        {"reason": "Investigation complete."}, dev_b_token, host="console.localhost", expect=(200,),
    )
    check(status == 200 and revoked["status"] == "revoked", "the grant is revoked", failures)

    step(14, "Elevation stops immediately - the same elevated read call now fails (401), not merely 'eventually'")
    status, _ = elevated_incidents_call(dev_a_token, tenant_id, expect=(401,))
    check(status == 401, "revoked grant no longer elevates -> 401", failures)

    step(15, "Clean up")
    psql(f"DELETE FROM tenants WHERE id IN ('{tenant_id}', '{other_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Support Grant Auth E2E {suffix}', 'Support Grant Auth E2E Other {suffix}')")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'owner-{suffix}@northwind.example', 'other-owner-{suffix}@northwind.example', "
        f"'{dev_a_email}', '{_dev_b_email}')"
    )
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "PASS - support-grant elevation grants real access, refuses without the header, refuses before "
        "approval, refuses for a wrong tenant, stops immediately on revoke, and the audit trail carries "
        "support_grant_id for a real elevated write"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
