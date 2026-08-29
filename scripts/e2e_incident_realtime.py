"""Proves the Customer CRM's incident inbox actually updates live, not just on refresh.

Builds on the same real-pipeline setup e2e_rule_to_incident.py already established
(register a tenant, a site, a camera, a zone, a rule, a device identity that can post
detections) and adds the part that matters here: with the Incidents page already open in
a real browser, a detection posted through the real ingestion endpoint has to appear as a
new row - and a status change made through the API has to update that row - with no
`page.reload()` anywhere in this script. If either of those needs a manual refresh to show
up, the feature does not work, no matter what the WebSocket code looks like in isolation.

It also exercises the WS ticket's two real security properties directly, from inside the
browser (a raw `new WebSocket(...)`, not the app's own hook): a ticket cannot be reused
for a second connection, and a connection missing a ticket - or presented from an origin
outside the CORS allowlist - is refused rather than silently accepted.

    python scripts/e2e_incident_realtime.py [--headed]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

API = "http://localhost:8080"
PASSWORD = "RealtimeE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def api(path, payload=None, token=None, method="POST", host="app.localhost", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": host}
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


# Runs inside the browser: opens a raw WebSocket (bypassing the app's own hook entirely)
# and resolves once it either opens or closes, so a Python assertion can look at what
# actually happened at the protocol level rather than trusting the app's UI to reflect it
# correctly - the UI is exactly what this attempt is trying to verify independently of.
_RAW_SOCKET_JS = """
async (url) => {
  return await new Promise((resolve) => {
    let settled = false;
    const finish = (result) => { if (!settled) { settled = true; resolve(result); } };
    const timer = setTimeout(() => finish({ opened: false, code: null, timedOut: true }), 4000);
    try {
      const ws = new WebSocket(url);
      ws.onopen = () => { finish({ opened: true, code: null, timedOut: false }); clearTimeout(timer); ws.close(); };
      ws.onclose = (event) => { finish({ opened: false, code: event.code, timedOut: false }); clearTimeout(timer); };
      ws.onerror = () => {};
    } catch (err) {
      finish({ opened: false, code: null, timedOut: false, threw: String(err) });
      clearTimeout(timer);
    }
  });
}
"""


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant and build a real alerting path (site, camera, zone, rule)")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Realtime E2E {suffix}",
        "email": f"ops-{suffix}@realtime.example",
        "password": PASSWORD,
        "display_name": "Realtime Tester",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Realtime Depot", "code": f"rt-{suffix}", "timezone": "Asia/Kolkata",
    }, token)
    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "Yard Camera", "code": f"cam-{suffix}",
    }, token)
    _, zone = api("/api/v1/tenant/zones", {
        "site_id": site["id"], "name": "Yard", "zone_type": "restricted",
        "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
    }, token)
    _, rule = api("/api/v1/tenant/rules", {
        "site_id": site["id"], "camera_id": camera["id"], "zone_id": zone["id"],
        "name": "Realtime test rule", "type_code": "zone.intrusion",
        "alertable_classes": ["person"], "min_confidence": 0.4, "severity": "high",
        "min_roi_overlap": 0.1,
    }, token)
    print(f"    tenant {tenant_id}, camera {camera['id']}, rule {rule['id']}")

    device_email = f"device-{suffix}@realtime.example"
    owner_email = f"ops-{suffix}@realtime.example"
    psql(
        f"INSERT INTO users (email_normalized, email_display, password_hash, "
        f"display_name, status, email_verified_at) "
        f"SELECT CAST('{device_email}' AS citext), CAST('{device_email}' AS text), "
        f"u.password_hash, 'Yard camera', 'active', now() "
        f"FROM users u WHERE u.email_normalized = CAST('{owner_email}' AS citext)"
    )
    psql(
        "INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
        f"SELECT '{tenant_id}', u.id, r.id, 'active', now() FROM users u, roles r "
        f"WHERE u.email_normalized = CAST('{device_email}' AS citext) "
        "AND r.name = 'edge_device' AND r.tenant_id IS NULL"
    )
    _, device_auth = api("/api/v1/auth/login", {"email": device_email, "password": PASSWORD})
    device_token = device_auth["access_token"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)

        try:
            step(2, "Sign in through the real UI and land on the incident inbox")
            page.goto("http://app.localhost:8080/login")
            page.get_by_label("Email").fill(owner_email)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/incidents", timeout=15000)

            step(3, "The live indicator actually connects, not just renders")
            live = page.locator(".live-indicator")
            live.wait_for(timeout=5000)
            # "Connecting…" is a legitimate first paint; give the real handshake (ticket
            # fetch + WS upgrade) a few seconds to land before treating it as a failure.
            for _ in range(20):
                if "Live" in live.inner_text():
                    break
                page.wait_for_timeout(250)
            check("Live" in live.inner_text(), f"live indicator reads connected ({live.inner_text()!r})", failures)

            step(4, "A detection posted through the real pipeline appears with no reload")
            source_event_id = f"e2e-realtime-{suffix}-1"
            _, result = api("/api/v1/tenant/ingest/detections", {
                "camera_id": camera["id"],
                "source_event_id": source_event_id,
                "captured_at": dt.datetime.now(dt.UTC).isoformat(),
                "objects": [
                    {"class_name": "person", "confidence": 0.9, "bbox": [0.2, 0.2, 0.4, 0.6]},
                ],
            }, device_token)
            check(result["incident_created"], "the detection actually opened a new incident", failures)
            incident_number = result["incident_number"]

            appeared = False
            for _ in range(20):  # up to ~5s: one poll tick (1s) plus real margin
                if page.get_by_text(f"#{incident_number} ·").count() > 0:
                    appeared = True
                    break
                page.wait_for_timeout(250)
            check(appeared, f"incident #{incident_number} appeared in the list with no reload", failures)

            step(5, "A toast announced the new incident")
            check(
                page.get_by_text("New incident").count() > 0,
                "a live-update toast was shown for the creation event", failures,
            )
            shot(page, "47-incident-realtime-created")

            step(6, "Acknowledging it through the API updates the row live, still no reload")
            api(f"/api/v1/tenant/incidents/{result['incident_id']}/acknowledge", {}, token)
            row = page.locator(".card", has_text=f"#{incident_number} ·")
            updated = False
            for _ in range(20):
                if row.locator("text=acknowledged").count() > 0:
                    updated = True
                    break
                page.wait_for_timeout(250)
            check(updated, "the row's status badge reflects the acknowledgement with no reload", failures)
            shot(page, "48-incident-realtime-acknowledged")

            step(7, "A ticket cannot be reused for a second connection")
            _, ticket_resp = api("/api/v1/tenant/realtime/ws-ticket", None, token)
            ticket = ticket_resp["ticket"]
            ws_url = f"ws://app.localhost:8080/ws/v1/tenant/incidents?ticket={ticket}"
            first = page.evaluate(_RAW_SOCKET_JS, ws_url)
            second = page.evaluate(_RAW_SOCKET_JS, ws_url)
            check(first["opened"], f"the ticket's first use connects ({first})", failures)
            check(not second["opened"], f"the same ticket's second use is refused ({second})", failures)

            step(8, "A connection with no ticket at all is refused")
            no_ticket = page.evaluate(_RAW_SOCKET_JS, "ws://app.localhost:8080/ws/v1/tenant/incidents")
            check(not no_ticket["opened"], f"a missing ticket is refused ({no_ticket})", failures)

            step(9, "A connection from an origin outside the CORS allowlist is refused")
            # tenant-api's own router matches by path, not Host - reachable on the bare
            # `localhost` origin too, which is deliberately NOT in customer_crm_origins.
            # Reusing this same detail is what makes the check possible without a second
            # real hostname.
            _, other_ticket_resp = api("/api/v1/tenant/realtime/ws-ticket", None, token)
            off_origin_page = browser.new_context().new_page()
            off_origin_page.goto(f"{API}/api/v1/tenant/openapi.json")
            off_origin_url = (
                f"ws://localhost:8080/ws/v1/tenant/incidents?ticket={other_ticket_resp['ticket']}"
            )
            off_origin = off_origin_page.evaluate(_RAW_SOCKET_JS, off_origin_url)
            check(not off_origin["opened"], f"an out-of-allowlist origin is refused ({off_origin})", failures)
            off_origin_page.close()

        finally:
            browser.close()

    step(10, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Realtime E2E {suffix}'")
    print("    test tenant removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the incident inbox updates live (creation and status changes), and the "
          "WS ticket's single-use and origin-check properties both hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
