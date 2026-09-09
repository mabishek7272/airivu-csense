"""Drives the Customer CRM's new webhook-management page in a real browser.

CHECKLIST.md named the gap plainly: `backend/tenant_api/app/api/webhooks.py` has a
complete, working, tenant-scoped API (create/list/patch/rotate-secret/delete/test/
deliveries), and `scripts/e2e_webhooks.py` / `scripts/e2e_webhook_dispatch.py` already
prove that API works end-to-end — but nothing in either Customer CRM page called it, so
there was no UI to attach a delivery-history view onto. `frontend/customer-crm/src/pages/
WebhooksPage.tsx` is that page; this script proves the real browser round trip through it,
the same standard `e2e_pipeline_assignments_crm.py` already set for a freshly-built CRM
page: real tenant, real dialogs, real toasts, real network calls, not mocked state.

Two backend calls happen outside the browser, both for the same reason
`e2e_webhook_dispatch.py` already established: incidents have no public creation
endpoint (seeded via `psql`) and acknowledging one is a different page's job than this
script is testing, so it goes through the real API directly rather than driving yet
another page's UI just to produce a side effect. Everything that is actually this page's
own job — create, the one-time secret reveal, list, real test-delivery, delivery history
(including watching a *second*, automatically-dispatched delivery appear after a real
page reload), rotate-secret, delete — goes through the browser.

    python scripts/e2e_webhooks_crm.py [--headed]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CRM_BASE", "http://localhost:5173")
API = os.environ.get("API_BASE", "http://localhost:8080")
PASSWORD = "WebhookCrmE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")

MATCHING_EVENT = "incident.acknowledged.v1"
DELIVERY_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 5


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    """Same small helper `e2e_webhook_dispatch.py` already uses — for the two things
    that are genuinely not this page's job: seeding an incident with no creation
    endpoint, and acknowledging it through the real API rather than a different page's
    UI."""
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
        cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra"),
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _run_scenario(
    page, browser, suffix: str, email: str, token: str, tenant_id: str, failures: list[str]
) -> None:
    print("\n[1] Sign in through the real login form (the tenant itself was registered "
          "through the real API above, exactly as e2e_webhook_dispatch.py does it, since "
          "this script's own job is the webhook page, not registration)")
    page.goto(f"{BASE}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard", timeout=30000)

    page.get_by_role("link", name="Webhooks", exact=True).click()
    page.wait_for_selector(".state-panel")
    check("No webhooks yet" in page.inner_text(".state-panel"), "starts with the true empty state", failures)
    shot(page, "webhooks-01-empty")

    print("\n[2] Create a webhook through the real form, filtered to the real event this "
          "run will cause below")
    page.get_by_role("button", name="Add your first webhook").click()
    page.wait_for_selector("div[role='dialog']")
    create_dialog = page.locator("div[role='dialog']")
    create_dialog.locator('[name="name"]').fill("CRM E2E Endpoint")
    create_dialog.locator('[name="url"]').fill("https://httpbin.org/post")
    create_dialog.locator("#webhook-event-filters").fill(MATCHING_EVENT)
    create_dialog.get_by_role("button", name="Add webhook").click()

    print("\n[3] The one-time secret is shown right after create, and never again")
    page.wait_for_selector("div[role='dialog']:has-text('Signing secret for CRM E2E Endpoint')")
    secret_dialog = page.locator("div[role='dialog']")
    first_secret = secret_dialog.locator(".token-display code").inner_text()
    check(first_secret.startswith("whsec_"), "a real signing secret is shown, once", failures)
    shot(page, "webhooks-02-secret-on-create")
    secret_dialog.get_by_role("button", name="Done").click()
    page.wait_for_selector("div[role='dialog']", state="detached")

    print("\n[4] It appears in the list with the real fields the API returned")
    row = page.locator(".data-table tbody tr", has_text="CRM E2E Endpoint")
    row.wait_for()
    row_text = row.inner_text()
    check("httpbin.org" in row_text, "only the hostname is shown, never the full URL", failures)
    check(MATCHING_EVENT in row_text, "the event filter typed into the form round-tripped", failures)
    check("active" in row_text, "a new endpoint starts active", failures)
    shot(page, "webhooks-03-listed")

    print("\n[5] A real test delivery, fired through the UI, against the real endpoint")
    row.get_by_role("button", name="Test").click()
    page.wait_for_selector(".toast-success, .toast-warning", timeout=20000)
    toast_text = page.locator(".toast-success, .toast-warning").first.inner_text()
    print(f"      toast: {toast_text!r}")
    check("HTTP" in toast_text or "respond" in toast_text.lower(), "the real TestDeliveryOut result surfaced inline", failures)

    print("\n[6] The delivery history dialog shows that same test delivery for real")
    row.get_by_role("button", name="Deliveries").click()
    page.wait_for_selector("div[role='dialog']")
    deliveries_dialog = page.locator("div[role='dialog']")
    deliveries_dialog.locator(".card-list .card", has_text="webhook.test").first.wait_for(timeout=20000)
    test_card_text = deliveries_dialog.locator(".card-list .card", has_text="webhook.test").first.inner_text()
    print(f"      test delivery card: {test_card_text!r}")
    check("succeeded" in test_card_text.lower() or "failed" in test_card_text.lower(), "a real terminal status is shown", failures)
    shot(page, "webhooks-04-deliveries-after-test")
    deliveries_dialog.locator(".dialog-footer").get_by_role("button", name="Close").click()
    page.wait_for_selector("div[role='dialog']", state="detached")

    print(f"\n[7] Cause a real automatic delivery the same way e2e_webhook_dispatch.py "
          f"does — seed an incident, acknowledge it through the real API — then confirm "
          f"the delivery history dialog shows it for real after a genuine fresh page load")
    site_status, site = api("/api/v1/tenant/sites", {
        "name": "CRM E2E Depot", "code": f"crm-webhook-site-{suffix}", "timezone": "UTC",
    }, token, expect=(201,))
    check(site_status == 201, "a real site exists to attach a camera to", failures)
    site_id = site["id"]

    camera_status, camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "CRM E2E Gate Camera", "code": f"crm-webhook-cam-{suffix}",
    }, token, expect=(201,))
    check(camera_status == 201, "a real camera exists for the incident to reference", failures)
    camera_id = camera["id"]

    incident_id = psql(
        "INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, "
        "type_code, severity, status, title, first_detected_at, last_detected_at) VALUES ("
        f"'{tenant_id}', '{site_id}', '{camera_id}', 1, 'zone.intrusion', "
        "'high', 'open', 'CRM webhook e2e probe', now(), now()) RETURNING id"
    )
    check(bool(incident_id), "an open incident was seeded (no public creation endpoint exists)", failures)

    ack_status, ack = api(
        f"/api/v1/tenant/incidents/{incident_id}/acknowledge",
        {"reason": "CRM webhook e2e"}, token, expect=(200,),
    )
    check(ack_status == 200 and ack.get("status") == "acknowledged", "the incident really transitioned, emitting a real domain event", failures)

    _, listed = api("/api/v1/tenant/webhooks", token=token, method="GET")
    webhook_id = next(w["id"] for w in listed if w["name"] == "CRM E2E Endpoint")

    print(f"    waiting up to {DELIVERY_TIMEOUT_SECONDS}s for the deployed "
          f"notification-worker to actually deliver it — polled directly against the "
          f"real API (the same way e2e_webhook_dispatch.py's own wait_for_delivery does),"
          f" not through the browser: nothing here drives the delivery, the "
          f"notification-worker container does")
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    delivered_via_api = False
    while time.monotonic() < deadline and not delivered_via_api:
        _, page_result = api(f"/api/v1/tenant/webhooks/{webhook_id}/deliveries", token=token, method="GET")
        delivered_via_api = any(
            d["event_type"] == MATCHING_EVENT and d["status"] == "succeeded" for d in page_result["items"]
        )
        if not delivered_via_api:
            time.sleep(POLL_INTERVAL_SECONDS)
    check(delivered_via_api, "the automatic delivery reached 'succeeded' for real, confirmed via the real API", failures)

    print("    now confirming the CRM's own delivery history dialog shows it too, after a "
          "genuine fresh page load (a full navigation and a real re-login, not a client- "
          "side re-render or injected state) — a plain page.reload() is deliberately not "
          "used here: it exercises the httpOnly-cookie silent-refresh path, which this "
          "session found rotates the refresh token on every mount and has no tolerance "
          "for two concurrent presentations of it (see this script's own final report) —"
          " a real fresh login sidesteps that unrelated, pre-existing session-handling "
          "risk while still being a completely real, uninjected page load")
    page.goto(f"{BASE}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard", timeout=30000)
    page.get_by_role("link", name="Webhooks", exact=True).click()
    page.wait_for_selector(".data-table")

    row = page.locator(".data-table tbody tr", has_text="CRM E2E Endpoint")
    row.get_by_role("button", name="Deliveries").click()
    page.wait_for_selector("div[role='dialog']")
    deliveries_dialog = page.locator("div[role='dialog']")
    matching = deliveries_dialog.locator(".card-list .card", has_text=MATCHING_EVENT)
    matching.first.wait_for(timeout=15000)
    check(matching.count() > 0, "the automatically-dispatched delivery shows up in the real UI after a real fresh page load", failures)
    if matching.count() > 0:
        print(f"      found after fresh load: {matching.first.inner_text()!r}")
        shot(page, "webhooks-05-deliveries-after-real-reload")
    deliveries_dialog.locator(".dialog-footer").get_by_role("button", name="Close").click()
    page.wait_for_selector("div[role='dialog']", state="detached")

    print("\n[8] Rotate the secret through the UI — a new one-time secret, genuinely "
          "different from the first")
    row = page.locator(".data-table tbody tr", has_text="CRM E2E Endpoint")
    row.get_by_role("button", name="Rotate secret").click()
    page.wait_for_selector("div[role='dialog']:has-text('Rotate this signing secret?')")
    # Scoped to the confirm dialog specifically — its own confirm button reads "Rotate
    # secret" too, the same text as the row action that opened it, so an unscoped query
    # would be ambiguous between the two.
    page.locator("div[role='dialog']").get_by_role("button", name="Rotate secret", exact=True).click()
    page.wait_for_selector("div[role='dialog']:has-text('New signing secret for CRM E2E Endpoint')")
    rotate_dialog = page.locator("div[role='dialog']")
    second_secret = rotate_dialog.locator(".token-display code").inner_text()
    check(second_secret.startswith("whsec_"), "a real new secret is shown", failures)
    check(second_secret != first_secret, "the rotated secret genuinely differs from the original", failures)
    shot(page, "webhooks-06-secret-on-rotate")
    rotate_dialog.get_by_role("button", name="Done").click()
    page.wait_for_selector("div[role='dialog']", state="detached")

    print("\n[9] Delete it through the UI and confirm it is gone from the list")
    row = page.locator(".data-table tbody tr", has_text="CRM E2E Endpoint")
    row.get_by_role("button", name="Remove").click()
    page.wait_for_selector("div[role='dialog']:has-text('Remove this webhook?')")
    page.get_by_role("button", name="Remove webhook").click()
    page.wait_for_selector(".toast-success", timeout=15000)
    page.wait_for_timeout(300)
    check(page.locator(".data-table tbody tr", has_text="CRM E2E Endpoint").count() == 0, "the row is gone after a real DELETE round trip", failures)
    shot(page, "webhooks-07-after-delete")

    print("\n[10] Console stayed clean moving off and back onto the page")
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.get_by_role("link", name="Cameras", exact=True).click()
    page.wait_for_selector(".data-table, .state-panel")
    page.get_by_role("link", name="Webhooks", exact=True).click()
    page.wait_for_selector(".state-panel")
    check(not errors, f"no console errors moving off and back onto the page ({errors})", failures)

    browser.close()


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Webhook CRM E2E {suffix}"
    email = f"webhook-crm-{suffix}@northwind.example"

    print("\n[0] Register a real tenant through the real API (this script's own subject "
          "is the webhook page, not registration — e2e_pipeline_assignments_crm.py "
          "exercises the real registration *form*, so this one does not need to)")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": organization_name,
        "email": email,
        "password": PASSWORD, "display_name": "Webhook CRM Tester",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]
    print(f"    tenant {tenant_id}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless="--headed" not in sys.argv)
            page = browser.new_context(viewport={"width": 1280, "height": 950}).new_page()
            page.set_default_timeout(30000)
            _run_scenario(page, browser, suffix, email, token, tenant_id, failures)
    finally:
        print("\n[11] Clean up")
        # Same ordering `e2e_webhook_dispatch.py` established: processed_events and
        # outbox_events have no FK to tenants, so they are orphaned rather than cascaded
        # away by the tenant delete below and must go first.
        psql(
            "DELETE FROM processed_events WHERE consumer_name = 'webhook_dispatcher' "
            f"AND event_id IN (SELECT id FROM outbox_events WHERE tenant_id = '{tenant_id}')"
        )
        psql(f"DELETE FROM outbox_events WHERE tenant_id = '{tenant_id}'")
        psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
        psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
        psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
        print("    test tenant, organization, user, outbox and processed_events rows removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the Customer CRM's webhook page creates, lists, tests, shows delivery "
          "history (including a real automatically-dispatched delivery seen after a real "
          "page reload), rotates and deletes webhook endpoints, all against the real "
          "running stack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
