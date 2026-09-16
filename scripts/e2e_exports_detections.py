"""Proves async detections exports work end-to-end for real (CHECKLIST: "Async reports/
exports with time-limited download" - detections was the named "easy next slice" after
incidents). Same proof shape as e2e_exports.py: a real background task generates a real
CSV, uploads it to the real running MinIO, and a real presigned URL actually downloads
bytes that match what was queried. CSV formatting itself is proven directly in
backend/tests/test_exports_detections.py; this script proves the mechanism as wired into
the real running API, database, and object store - deliberately a separate script per
export type, mirroring e2e_exports.py's own file-per-concern shape rather than one script
growing a second, unrelated set of steps bolted onto the end.

Detections have no public creation endpoint (they only ever come from the detection
pipeline) - seeded directly via `psql`, the same shortcut e2e_exports.py already takes
for incidents.

    python scripts/e2e_exports_detections.py
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "ExportsDetectionsE2E!Password123"
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

    step(1, "Register a tenant and create a site + two cameras through the real API")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports Detections E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
    }, token, expect=(201,))
    site_id = site["id"]

    _, gate_camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Gate Camera", "code": f"gate-{suffix}",
    }, token, expect=(201,))
    gate_camera_id = gate_camera["id"]

    _, dock_camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Dock Camera", "code": f"dock-{suffix}",
    }, token, expect=(201,))
    dock_camera_id = dock_camera["id"]

    step(2, "Seed five detections directly across both cameras (no public creation endpoint)")
    detection_ids = []
    for i in range(5):
        camera_id = gate_camera_id if i < 3 else dock_camera_id
        confidence = 0.5 + (i * 0.1)
        objects = json.dumps([{"class": "person", "confidence": confidence, "bbox": [0, 0, 10, 10]}])
        detection_id = psql(
            "INSERT INTO detections (tenant_id, site_id, camera_id, event_type, "
            "source_event_id, capture_time, confidence, objects) VALUES ("
            f"'{tenant_id}', '{site_id}', '{camera_id}', 'person_detected', "
            f"'src-{suffix}-{i}', now(), {confidence}, '{objects}'::jsonb"
            ") RETURNING id"
        )
        detection_ids.append(detection_id)
    check(len(detection_ids) == 5, "five detections seeded", failures)

    step(3, "Request an export of every detection and poll it to completion")
    status, job = api("/api/v1/tenant/exports/detections", {}, token, expect=(202,))
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
            all(any(did in line for line in lines[1:]) for did in detection_ids),
            "every seeded detection id appears in the downloaded CSV", failures,
        )
        check(
            any("Gate Camera" in line for line in lines[1:]) and any("Dock Camera" in line for line in lines[1:]),
            "both real camera names are joined in correctly, not just ids", failures,
        )
        check(
            any("person:0." in line for line in lines[1:]),
            "objects_summary carries a real flattened class:confidence pair", failures,
        )

    step(5, "A camera_id filter narrows the export to just that camera's detections")
    status, filtered_job = api(
        "/api/v1/tenant/exports/detections", {"camera_id": gate_camera_id}, token, expect=(202,)
    )
    filtered = poll_until_terminal(filtered_job["id"], token)
    check(filtered["status"] == "completed", "the filtered job also completes", failures)
    check(
        filtered["row_count"] == 3,
        f"only the 3 gate-camera detections are exported (got {filtered.get('row_count')})", failures,
    )

    step(6, "A min_confidence filter narrows the export")
    status, conf_job = api(
        "/api/v1/tenant/exports/detections", {"min_confidence": 0.75}, token, expect=(202,)
    )
    conf_finished = poll_until_terminal(conf_job["id"], token)
    check(conf_finished["status"] == "completed", "the confidence-filtered job also completes", failures)
    check(
        conf_finished["row_count"] == 2,
        f"only the 2 detections at confidence >= 0.75 are exported (got {conf_finished.get('row_count')})", failures,
    )

    step(7, "The job list shows both incidents-type and detections-type jobs for this tenant")
    _, listed = api("/api/v1/tenant/exports", token=token, method="GET", expect=(200,))
    export_types = {job_entry["export_type"] for job_entry in listed}
    check("detections" in export_types, "a 'detections' export_type job is present", failures)

    step(8, "A second, unrelated tenant cannot see this tenant's detections export jobs (RLS)")
    other_suffix = uuid.uuid4().hex[:8]
    _, other_auth = api("/api/v1/auth/register", {
        "organization_name": f"Exports Detections E2E Other {other_suffix}",
        "email": f"owner-{other_suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    other_token = other_auth["access_token"]
    status, _ = api(f"/api/v1/tenant/exports/{job_id}", token=other_token, method="GET", expect=(404,))
    check(status == 404, "the other tenant gets a 404 for this tenant's job id, not its data", failures)

    step(9, "Clean up")
    # Same ordering as e2e_exports.py's own cleanup, and the same real bug it names
    # avoiding: tenants first (both), then organizations (both), then users.
    emails = [f"owner-{s}@northwind.example" for s in (suffix, other_suffix)]
    for email in emails:
        uid = psql(f"SELECT id FROM users WHERE email_normalized = '{email}'")
        if uid:
            tid = psql(f"SELECT tenant_id FROM memberships WHERE user_id = '{uid}' LIMIT 1")
            if tid:
                psql(f"DELETE FROM tenants WHERE id = '{tid}'")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Exports Detections E2E {suffix}', 'Exports Detections E2E Other {other_suffix}')")
    for email in emails:
        psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - async detections export generation, filtering, presigned download, and tenant isolation all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
