"""Proves camera health telemetry history (CHECKLIST: "Camera health current-state model
+ telemetry history") is durable and correct, against the deployment's own real,
DNS-resolvable NVR host (see [[nvr-h265-constraint]] - same host `e2e_live_view.py` uses;
structural verification runs with no credentials needed, same as that script's own).

Each real probe is a real network round trip - authenticated or not, its outcome (reachable
or not) is what gets written to `camera_health_events`. This checks the *mechanism*
(history accumulates, in order, matches the probe's own outcome, and a blocked/SSRF-refused
probe attempt writes nothing at all), not a specific reachability result.

    python scripts/e2e_camera_health.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "CameraHealthE2E!Password123"

NVR_HOST = os.environ.get("TEST_NVR_HOST", "autotek-dorani-nvr.dyndns.org")
NVR_PORT = int(os.environ.get("TEST_NVR_PORT", "554"))
NVR_MAIN_PATH = os.environ.get("TEST_NVR_MAIN_PATH", "/unicast/c3/s0/live")


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
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


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant, add a site and a camera pointed at the real NVR")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Camera Health E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Ops",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {"name": f"Site {suffix}", "code": f"site-{suffix}"}, token, expect=(201,))
    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "NVR Camera", "code": "nvr-cam",
        "hostname": NVR_HOST, "rtsp_port": NVR_PORT, "main_stream_path": NVR_MAIN_PATH,
    }, token, expect=(201,))
    camera_id = camera["id"]

    step(2, "Before any probe, health is unknown with no history")
    _, health0 = api(f"/api/v1/tenant/cameras/{camera_id}/health", token=token, method="GET")
    check(health0["current_status"] == "unknown", "no probe yet -> unknown, not online/offline", failures)
    check(health0["history"] == [], "no history yet", failures)

    step(3, "A real probe against the real NVR host writes a real health event")
    _, probe1 = api(f"/api/v1/tenant/cameras/{camera_id}/probe", {}, token)
    print(f"    probe outcome: reachable={probe1['reachable']}: {probe1['detail']}")

    _, health1 = api(f"/api/v1/tenant/cameras/{camera_id}/health", token=token, method="GET")
    expected_status = "online" if probe1["reachable"] else "offline"
    check(health1["current_status"] == expected_status, "current_status matches the real probe outcome", failures)
    check(len(health1["history"]) == 1, "exactly one history event after one probe", failures)
    check(
        health1["history"][0]["status"] == expected_status,
        "the history event's own status matches too", failures,
    )

    step(4, "A second real probe adds a second event, newest first")
    api(f"/api/v1/tenant/cameras/{camera_id}/probe", {}, token)
    _, health2 = api(f"/api/v1/tenant/cameras/{camera_id}/health", token=token, method="GET")
    check(len(health2["history"]) == 2, "two probes -> two history events", failures)
    check(
        health2["history"][0]["occurred_at"] >= health2["history"][1]["occurred_at"],
        "newest event first", failures,
    )

    step(5, "An SSRF-refused probe attempt writes nothing at all")
    _, internal_camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "Internal", "code": "internal-cam",
        "hostname": "127.0.0.1", "rtsp_port": 554, "main_stream_path": "/live",
    }, token, expect=(201,))
    status, _ = api(f"/api/v1/tenant/cameras/{internal_camera['id']}/probe", {}, token, expect=(422,))
    check(status == 422, "probing a private address is still refused (422)", failures)
    _, internal_health = api(f"/api/v1/tenant/cameras/{internal_camera['id']}/health", token=token, method="GET")
    check(
        internal_health["current_status"] == "unknown" and internal_health["history"] == [],
        "a blocked probe attempt leaves health untouched - nothing was actually probed", failures,
    )

    step(6, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Camera Health E2E {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'ops-{suffix}@northwind.example'")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - camera health telemetry history is durable, ordered, and matches real probe outcomes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
