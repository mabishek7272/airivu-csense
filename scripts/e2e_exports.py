"""Proves async incident exports work end-to-end for real (CHECKLIST: "Async reports/
exports with time-limited download"): a real background task generates a real CSV,
uploads it to the real running MinIO, and a real presigned URL actually downloads bytes
that match what was queried - not just that the job's status flips to "completed". CSV
formatting itself is proven directly in backend/tests/test_exports.py; this script proves
the mechanism as wired into the real running API, database, and object store.

Incidents have no public creation endpoint (they only ever come from the detection
pipeline) - seeded directly via `psql`, the same shortcut e2e_webhooks.py already takes
for things outside what this script is actually testing.

    python scripts/e2e_exports.py
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "ExportsE2E!Password123"
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

    step(1, "Register a tenant and create a site + camera through the real API")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
    }, token, expect=(201,))
    site_id = site["id"]

    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Gate Camera", "code": f"gate-{suffix}",
    }, token, expect=(201,))
    camera_id = camera["id"]

    step(2, "Seed five incidents directly (no public creation endpoint - see this script's own docstring)")
    incident_ids = []
    for i in range(5):
        severity = "critical" if i == 0 else "high" if i < 3 else "low"
        status = "resolved" if i < 2 else "open"
        incident_id = psql(
            "INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, "
            "type_code, severity, status, title, first_detected_at, last_detected_at"
            + (", resolution_code" if status == "resolved" else "")
            + ") VALUES ("
            f"'{tenant_id}', '{site_id}', '{camera_id}', {i + 1}, 'zone.intrusion', "
            f"'{severity}', '{status}', 'Intrusion #{i + 1}', now(), now()"
            + (", 'confirmed_true_positive'" if status == "resolved" else "")
            + ") RETURNING id"
        )
        incident_ids.append(incident_id)
    check(len(incident_ids) == 5, "five incidents seeded", failures)

    step(3, "Request an export of every incident and poll it to completion")
    status, job = api("/api/v1/tenant/exports/incidents", {}, token, expect=(202,))
    check(status == 202, "the request is accepted immediately (202)", failures)
    check(job["status"] == "queued", "the job starts out queued", failures)
    job_id = job["id"]

    finished = poll_until_terminal(job_id, token)
    check(finished["status"] == "completed", f"the job reaches 'completed' (got '{finished.get('status')}')", failures)
    check(finished["row_count"] == 5, f"row_count is 5 (got {finished.get('row_count')})", failures)
    check(finished["download_url"] is not None, "a download URL is present once completed", failures)

    step(4, "The presigned URL actually downloads real, correct CSV bytes")
    if finished.get("download_url"):
        with urllib.request.urlopen(finished["download_url"], timeout=30) as response:
            csv_bytes = response.read()
        text = csv_bytes.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line]
        check(len(lines) == 6, f"header + 5 data rows (got {len(lines)} lines)", failures)
        check(lines[0].split(",")[0] == "id", "header starts with the 'id' column", failures)
        check(
            all(any(iid in line for line in lines[1:]) for iid in incident_ids),
            "every seeded incident id appears in the downloaded CSV", failures,
        )

    step(5, "A status filter narrows the export")
    status, filtered_job = api("/api/v1/tenant/exports/incidents", {"status": "resolved"}, token, expect=(202,))
    filtered = poll_until_terminal(filtered_job["id"], token)
    check(filtered["status"] == "completed", "the filtered job also completes", failures)
    check(filtered["row_count"] == 2, f"only the 2 resolved incidents are exported (got {filtered.get('row_count')})", failures)

    step(6, "An invalid status filter is refused before any job is created")
    status, refusal = api("/api/v1/tenant/exports/incidents", {"status": "not-a-real-status"}, token, expect=(400,))
    check(status == 400, "invalid status is refused (400)", failures)
    check(refusal.get("code") == "invalid_status", "clear refusal code", failures)

    step(7, "The job list shows this tenant's jobs, newest first")
    _, listed = api("/api/v1/tenant/exports", token=token, method="GET", expect=(200,))
    check(len(listed) >= 2, "at least the two real jobs appear", failures)
    check(listed[0]["id"] in (job_id, filtered_job["id"]), "newest job leads the list", failures)

    step(8, "A bogus job id is a real 404, not a silent empty result")
    status, _ = api(f"/api/v1/tenant/exports/{uuid.uuid4()}", token=token, method="GET", expect=(404,))
    check(status == 404, "an unknown job id 404s", failures)

    step(9, "A second, unrelated tenant cannot see this tenant's export jobs (RLS)")
    other_suffix = uuid.uuid4().hex[:8]
    _, other_auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports E2E Other {other_suffix}",
        "email": f"owner-{other_suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    other_token = other_auth["access_token"]
    status, _ = api(f"/api/v1/tenant/exports/{job_id}", token=other_token, method="GET", expect=(404,))
    check(status == 404, "the other tenant gets a 404 for this tenant's job id, not its data", failures)
    _, other_listed = api("/api/v1/tenant/exports", token=other_token, method="GET", expect=(200,))
    check(other_listed == [], "the other tenant's own job list is empty", failures)

    step(10, "Clean up")
    # Tenants first (both), then organizations (both), then users - deleting an
    # organization while its own tenant still exists is a real FK violation, and a
    # single multi-row DELETE is atomic, so doing this per-suffix inside one loop (an
    # earlier version of this script's own real bug) rolled back an already-successful
    # deletion alongside the one that failed.
    emails = [f"owner-{s}@northwind.example" for s in (suffix, other_suffix)]
    for email in emails:
        # A user has no direct tenant_id column - the relationship is via memberships,
        # the same schema e2e_memberships-style scripts already rely on.
        uid = psql(f"SELECT id FROM users WHERE email_normalized = '{email}'")
        if uid:
            tid = psql(f"SELECT tenant_id FROM memberships WHERE user_id = '{uid}' LIMIT 1")
            if tid:
                psql(f"DELETE FROM tenants WHERE id = '{tid}'")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Exports E2E {suffix}', 'Exports E2E Other {other_suffix}')")
    for email in emails:
        psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - async export generation, filtering, presigned download, and tenant isolation all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
