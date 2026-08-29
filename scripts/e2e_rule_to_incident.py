"""Proves a rule created through the API actually drives the pipeline it was built for.

Rules CRUD is the last piece of "configure everything from the UI, no SQL". The API and
the browser are both worth trusting only if a rule created through them behaves exactly
like the ones the pipeline tests already cover - so this creates one over HTTP, then posts
a detection through the real ingestion endpoint and checks an incident actually opens.

It also pins the two properties the API adds on top of the stored geometry:

  a rule pointing at a camera on a different site is refused before it is saved
  a high confidence threshold is reported back as a warning, not silently accepted

    python scripts/e2e_rule_to_incident.py
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "RuleE2E!Password123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
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


def tiny_jpeg() -> str:
    """A minimal valid JPEG, base64-encoded - the ingestion endpoint accepts a frame but
    does not require one to open an incident, so this just proves it does not choke on one."""
    import cv2
    import numpy as np

    image = np.full((200, 200, 3), 60, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register, create a site, a second site, a camera and a zone")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Rule E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Rule Tester",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site_a = api("/api/v1/tenant/sites", {
        "name": "Chennai Depot", "code": f"chennai-{suffix}", "timezone": "Asia/Kolkata",
    }, token)
    _, site_b = api("/api/v1/tenant/sites", {
        "name": "Mumbai Warehouse", "code": f"mumbai-{suffix}", "timezone": "Asia/Kolkata",
    }, token)

    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site_a["id"], "name": "Loading Bay 2", "code": f"bay-{suffix}",
    }, token)

    _, zone = api("/api/v1/tenant/zones", {
        "site_id": site_a["id"], "name": "Restricted dock", "zone_type": "restricted",
        "polygon": [[0.35, 0.30], [1.0, 0.30], [1.0, 1.0], [0.35, 1.0]],
    }, token)
    print(f"    site {site_a['id']}, camera {camera['id']}, zone {zone['id']}")

    step(2, "A rule pointing at another site's camera is refused")
    status, body = api("/api/v1/tenant/rules", {
        "site_id": site_b["id"], "camera_id": camera["id"],
        "name": "Cross-site rule", "alertable_classes": ["person"],
    }, token, expect=(422,))
    check(status == 422, "cross-site camera reference refused before saving", failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(3, "A high confidence threshold is reported back as a warning")
    _, risky = api("/api/v1/tenant/rules", {
        "site_id": site_a["id"], "camera_id": camera["id"], "zone_id": zone["id"],
        "name": "Daytime-only rule", "alertable_classes": ["person"],
        "min_confidence": 0.9,
    }, token)
    check(
        any("night" in w for w in risky["warnings"]),
        "a high threshold is flagged rather than silently accepted",
        failures,
    )
    api(f"/api/v1/tenant/rules/{risky['id']}", None, token, method="DELETE")

    step(4, "Create the real rule: person in the dock, at a workable threshold")
    _, rule = api("/api/v1/tenant/rules", {
        "site_id": site_a["id"], "camera_id": camera["id"], "zone_id": zone["id"],
        "name": "No entry to the dock", "type_code": "zone.intrusion",
        "alertable_classes": ["person"], "min_confidence": 0.4, "severity": "high",
        "min_roi_overlap": 0.3,
    }, token)
    check(rule["warnings"] == [], "a sensible rule carries no warnings", failures)
    print(f"    rule {rule['id']}")

    step(5, "Create a device identity that can post detections")
    # A token carries exactly one membership - `get_first_active_membership` resolves to
    # whichever exists, which would be the owner's if a second role were added to that
    # same user. So the device gets its own identity, the way an enrolled device would.
    device_email = f"device-{suffix}@northwind.example"
    owner_email = f"ops-{suffix}@northwind.example"
    psql(
        f"INSERT INTO users (email_normalized, email_display, password_hash, "
        f"display_name, status, email_verified_at) "
        f"SELECT CAST('{device_email}' AS citext), CAST('{device_email}' AS text), "
        f"u.password_hash, 'Loading Bay 2 camera', 'active', now() "
        f"FROM users u WHERE u.email_normalized = CAST('{owner_email}' AS citext)"
    )
    psql(
        "INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
        f"SELECT '{tenant_id}', u.id, r.id, 'active', now() FROM users u, roles r "
        f"WHERE u.email_normalized = CAST('{device_email}' AS citext) "
        "AND r.name = 'edge_device' AND r.tenant_id IS NULL"
    )
    _, device_auth = api("/api/v1/auth/login", {
        "email": device_email, "password": PASSWORD,
    })
    device_token = device_auth["access_token"]

    step(6, "Post a detection inside the zone - an incident should open")
    _, result = api("/api/v1/tenant/ingest/detections", {
        "camera_id": camera["id"],
        "source_event_id": f"e2e-{suffix}-1",
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [
            {"class_name": "person", "confidence": 0.85, "bbox": [0.44, 0.36, 0.55, 0.92]},
        ],
        "frame_base64": tiny_jpeg(),
    }, device_token)
    check(result["incident_created"], "the rule created through the API opened an incident", failures)
    check(result["rules_evaluated"] >= 1, "the rule was actually evaluated", failures)
    print(f"    incident #{result.get('incident_number')}  "
          f"created={result['incident_created']}  evidence={result['evidence_captured']}")

    step(7, "A detection outside the zone does not trigger it")
    _, miss = api("/api/v1/tenant/ingest/detections", {
        "camera_id": camera["id"],
        "source_event_id": f"e2e-{suffix}-2",
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [
            {"class_name": "person", "confidence": 0.9, "bbox": [0.02, 0.36, 0.12, 0.92]},
        ],
    }, device_token)
    check(not miss["incident_created"], "outside the zone, the same rule does not fire", failures)

    step(8, "Disabling the rule stops it firing on a fresh detection")
    api(f"/api/v1/tenant/rules/{rule['id']}", {"status": "disabled"}, token, method="PATCH")
    _, after_disable = api("/api/v1/tenant/ingest/detections", {
        "camera_id": camera["id"],
        "source_event_id": f"e2e-{suffix}-3",
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [
            {"class_name": "person", "confidence": 0.85, "bbox": [0.44, 0.36, 0.55, 0.92]},
        ],
    }, device_token)
    check(
        not after_disable["incident_created"],
        "a disabled rule is not evaluated - ingestion honours rule.status",
        failures,
    )

    step(9, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Rule E2E {suffix}'")
    print("    test tenant removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - a rule built through the API drives the real detection pipeline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
