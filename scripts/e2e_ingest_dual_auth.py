"""Proves detection ingestion's two supported auth paths both work for real, and that a
real device credential's own identity always wins over whatever the request body claims
(see ingest.py's own docstring for why both paths exist rather than one replacing the
other):

  - the legacy scoped-token path (an `edge_device`-role membership's own login token,
    carrying `detection.ingest` and nothing else) still works exactly as it did before -
    no regression from wiring in the second path
  - a real enrolled device's own credential (the same one `/heartbeat` and the signed-
    command endpoints already use) can post detections too, and the stored row's
    `edge_device_id` is the credential's own device - never the body's self-reported
    value, which this test deliberately sets to something else to prove it's ignored
  - a request with no valid credential at all gets one uniform 401, whichever path it
    might have been trying

    python scripts/e2e_ingest_dual_auth.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "IngestDualAuthE2E!Password123"


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


def detection(camera_id: str, source_event_id: str) -> dict:
    return {
        "camera_id": camera_id,
        "source_event_id": source_event_id,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.9, "bbox": [0.1, 0.1, 0.4, 0.4]}],
    }


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant, add a site and a camera")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Ingest Dual Auth E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Ops",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {"name": f"Site {suffix}", "code": f"site-{suffix}"}, owner_token, expect=(201,))
    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "Dock Cam", "code": "dock-cam",
    }, owner_token, expect=(201,))
    camera_id = camera["id"]

    step(2, "The legacy scoped-token path still works - an edge_device-role membership")
    device_email = f"device-{suffix}@northwind.example"
    psql(
        "INSERT INTO users (email_normalized, email_display, password_hash, display_name, status, email_verified_at) "
        f"SELECT CAST('{device_email}' AS citext), CAST('{device_email}' AS text), u.password_hash, "
        "'Legacy Device', 'active', now() FROM users u "
        f"WHERE u.email_normalized = CAST('ops-{suffix}@northwind.example' AS citext)"
    )
    device_user_id = psql(f"SELECT id FROM users WHERE email_normalized = CAST('{device_email}' AS citext)")
    psql(
        f"INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
        f"SELECT '{tenant_id}', '{device_user_id}', r.id, 'active', now() FROM roles r "
        "WHERE r.name = 'edge_device' AND r.tenant_id IS NULL"
    )
    _, device_auth = api("/api/v1/auth/login", {"email": device_email, "password": PASSWORD})
    legacy_token = device_auth["access_token"]

    self_reported_device_id = str(uuid.uuid4())
    status, _legacy_result = api("/api/v1/tenant/ingest/detections", {
        **detection(camera_id, f"legacy-{suffix}"), "edge_device_id": self_reported_device_id,
    }, legacy_token, expect=(202,))
    check(status == 202, "the legacy scoped-token path still accepts a detection", failures)

    stored_edge_device_id = psql(
        f"SELECT edge_device_id FROM detections WHERE tenant_id = '{tenant_id}' "
        f"AND source_event_id = 'legacy-{suffix}'"
    )
    check(
        stored_edge_device_id == self_reported_device_id,
        "the legacy path still trusts the body's self-reported edge_device_id, as before", failures,
    )

    step(3, "A real enrolled device can post too, and its OWN identity wins over the body")
    _, device = api("/api/v1/tenant/edge/devices", {
        "name": f"Real Device {suffix}", "device_type": "jetson_orin", "role": "inference",
    }, owner_token, expect=(201,))
    real_device_id = device["id"]
    _, issued = api(f"/api/v1/tenant/edge/devices/{real_device_id}/enrolment-token", {}, owner_token, expect=(201,))
    _, enrolled = api("/api/v1/tenant/edge/enrol", {
        "token": issued["token"], "serial_number": f"JETSON-{suffix.upper()}", "device_type": "jetson_orin",
    }, expect=(201,))
    agent_token = enrolled["agent_token"]

    status, _agent_result = api("/api/v1/tenant/ingest/detections", {
        # Deliberately claims a DIFFERENT device than the one actually authenticating -
        # this must be ignored in favour of the credential's own identity.
        **detection(camera_id, f"device-{suffix}"), "edge_device_id": self_reported_device_id,
    }, agent_token, expect=(202,))
    check(status == 202, "the real device credential can post a detection too", failures)

    stored_real_edge_device_id = psql(
        f"SELECT edge_device_id FROM detections WHERE tenant_id = '{tenant_id}' "
        f"AND source_event_id = 'device-{suffix}'"
    )
    check(
        stored_real_edge_device_id == real_device_id,
        "the stored edge_device_id is the credential's own device, not the body's claim", failures,
    )
    check(stored_real_edge_device_id != self_reported_device_id, "the body's claimed device_id was ignored", failures)

    step(4, "No credential at all is refused with one uniform 401")
    status, refusal = api("/api/v1/tenant/ingest/detections", detection(camera_id, f"none-{suffix}"), expect=(401,))
    check(status == 401, "an unauthenticated request is refused (401)", failures)
    check(refusal.get("code") in ("authentication_required", "device_unauthenticated"), "a clear auth failure code", failures)

    step(5, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Ingest Dual Auth E2E {suffix}'")
    psql(
        "DELETE FROM users WHERE email_normalized IN "
        f"(CAST('ops-{suffix}@northwind.example' AS citext), CAST('{device_email}' AS citext))"
    )
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - both ingestion auth paths work for real, and a real device credential always wins over the body")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
