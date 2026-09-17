"""Proves async audit-events exports work end-to-end for real (CHECKLIST: "Async reports/
exports with time-limited download" - audit-events was the second of the two remaining
"easy next slice" export types, after detections). Same proof shape as e2e_exports.py and
e2e_exports_detections.py: a real background task generates a real CSV, uploads it to the
real running MinIO, and a real presigned URL actually downloads bytes that match what was
queried. CSV formatting itself is proven directly in
backend/tests/test_exports_audit_events.py; this script proves the mechanism as wired
into the real running API, database, and object store.

**What actually writes a real audit event, checked directly rather than assumed**:
`grep -rl record_audit_and_outbox backend/tenant_api/app/api/` shows `auth.py`
(registration -> `tenant.created`) and `memberships.py` (`membership.invite`) do;
`cameras.py`/`sites.py` do NOT - creating a camera or site writes no audit event in this
codebase today. An earlier draft of this script assumed otherwise and asserted a
`camera.create` row that was never going to exist; fixed to seed two real events this
tenant genuinely produces (`tenant.created` from registration, `membership.invite` from a
real invite call) rather than events it doesn't.

**A permission boundary this script does NOT claim, corrected after a real check**: an
earlier draft assumed `audit.read` was `tenant_owner`-only, the same wrong assumption
`exports.py`'s own module docstring now documents catching. A direct query against
`role_permissions` shows `tenant_member` currently holds `audit.read` too, same as
`incident.read` - so a real `tenant_member`, seeded directly via `psycopg` with a real
password hash and a real `active` membership (mirroring `e2e_audit_log.py`'s own
`bootstrap_platform_admin` pattern), is expected to succeed at requesting and reading this
export, not be refused. What this script verifies instead: the member really can, for
real, exercising `exports.py`'s `_EXPORT_TYPE_PERMISSION` mapping on its true-today path
(every type currently permitted) rather than its defensive, not-currently-live path.

    python scripts/e2e_exports_audit_events.py
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password

API = "http://localhost:8080"
PASSWORD = "ExportsAuditE2E!Password123"
POLL_TIMEOUT_SECONDS = 20


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 202, 204)):
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
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _pg_dsn() -> str:
    settings = get_settings()
    return (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )


def seed_tenant_member(tenant_id: str, suffix: str) -> str:
    """Directly inserts a real, active `tenant_member` - mirrors e2e_audit_log.py's own
    `bootstrap_platform_admin` pattern (real bcrypt hash via the shared password module,
    not a fixture), adapted for a tenant-scoped membership rather than a platform role."""
    settings = get_settings()
    email = f"member-{suffix}@northwind.example"
    with psycopg.connect(_pg_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Member') RETURNING id",
            (email, email, hash_password(PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE audience = 'customer' AND name = 'tenant_member'"
        )
        role_id = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
        cur.execute(
            "INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
            "VALUES (%s, %s, %s, 'active', now())",
            (tenant_id, user_id, role_id),
        )
        conn.commit()
    return email


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def poll_until_terminal(job_id: str, token: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last = {}
    while time.monotonic() < deadline:
        _, last = api(f"/api/v1/tenant/exports/{job_id}", token=token, method="GET", expect=(200,))
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant (writes a real tenant.created audit event)")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports Audit E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "Seed a real tenant_member directly (psycopg, real password hash, real active membership)")
    member_email = seed_tenant_member(tenant_id, suffix)
    _, member_auth = api("/api/v1/auth/login", {"email": member_email, "password": PASSWORD}, expect=(200,))
    member_token = member_auth["access_token"]
    check(member_token, "the seeded member can actually log in", failures)

    step(3, "Invite a second, throwaway address through the real API (writes a real membership.invite event)")
    api("/api/v1/tenant/memberships", {
        "email": f"invitee-{suffix}@northwind.example", "display_name": "Invitee",
        "role_name": "tenant_member",
    }, owner_token, expect=(201,))

    step(4, "The owner requests an export of every audit event and polls it to completion")
    status, job = api("/api/v1/tenant/exports/audit-events", {}, owner_token, expect=(202,))
    check(status == 202, "the request is accepted immediately (202)", failures)
    check(job["status"] == "queued", "the job starts out queued", failures)
    job_id = job["id"]

    finished = poll_until_terminal(job_id, owner_token)
    check(finished["status"] == "completed", f"the job reaches 'completed' (got '{finished.get('status')}')", failures)
    check(finished["row_count"] >= 2, f"row_count covers at least the 2 real seeded events (got {finished.get('row_count')})", failures)
    check(finished["download_url"] is not None, "a download URL is present once completed", failures)

    step(5, "The presigned URL downloads real CSV bytes with real actor names and actions")
    if finished.get("download_url"):
        with urllib.request.urlopen(finished["download_url"], timeout=30) as response:
            csv_bytes = response.read()
        text = csv_bytes.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line]
        check(lines[0].split(",")[0] == "id", "header starts with the 'id' column", failures)
        check(any("tenant.created" in line for line in lines[1:]), "a real tenant.created row is present", failures)
        check(any("membership.invite" in line for line in lines[1:]), "a real membership.invite row is present", failures)
        check(any("Owner" in line for line in lines[1:]), "the real actor_display_name ('Owner') is joined in, not just an id", failures)

    step(6, "An action filter narrows the export")
    status, filtered_job = api(
        "/api/v1/tenant/exports/audit-events", {"action": "tenant.created"}, owner_token, expect=(202,)
    )
    filtered = poll_until_terminal(filtered_job["id"], owner_token)
    check(filtered["status"] == "completed", "the filtered job also completes", failures)
    check(filtered["row_count"] == 1, f"only the 1 tenant.created event is exported (got {filtered.get('row_count')})", failures)

    step(7, "A real tenant_member - who holds audit.read too, confirmed by direct query - can request the same export")
    status, member_job = api("/api/v1/tenant/exports/audit-events", {}, member_token, expect=(202,))
    check(status == 202, "the member is genuinely permitted, not refused", failures)
    member_finished = poll_until_terminal(member_job["id"], member_token)
    check(member_finished["status"] == "completed", "the member's own job completes too", failures)

    step(8, "The member's job list includes both their own and the owner's audit-events jobs (shared tenant scope)")
    _, member_listed = api("/api/v1/tenant/exports", token=member_token, method="GET", expect=(200,))
    member_job_ids = {j["id"] for j in member_listed}
    check(job_id in member_job_ids, "the owner's audit-events job is visible to the member", failures)
    check(member_job["id"] in member_job_ids, "the member's own audit-events job is visible too", failures)

    step(9, "A second, unrelated tenant cannot see this tenant's audit-events export jobs (RLS)")
    other_suffix = uuid.uuid4().hex[:8]
    _, other_auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports Audit E2E Other {other_suffix}",
        "email": f"owner-{other_suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    other_token = other_auth["access_token"]
    status, _ = api(f"/api/v1/tenant/exports/{job_id}", token=other_token, method="GET", expect=(404,))
    check(status == 404, "the other tenant gets a 404 for this tenant's job id, not its data", failures)

    step(10, "Clean up")
    emails = [f"owner-{s}@northwind.example" for s in (suffix, other_suffix)] + [
        member_email, f"invitee-{suffix}@northwind.example",
    ]
    for email in emails:
        uid = psql(f"SELECT id FROM users WHERE email_normalized = '{email}'")
        if uid:
            tid = psql(f"SELECT tenant_id FROM memberships WHERE user_id = '{uid}' LIMIT 1")
            if tid:
                psql(f"DELETE FROM tenants WHERE id = '{tid}'")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Exports Audit E2E {suffix}', 'Exports Audit E2E Other {other_suffix}')")
    for email in emails:
        psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - async audit-events export generation, filtering, presigned download, the")
    print("       real (shared) permission grant, and cross-tenant isolation all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
