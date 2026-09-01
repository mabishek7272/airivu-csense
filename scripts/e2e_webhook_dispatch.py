"""Proves *automatic*, outbox-driven webhook delivery works end-to-end for real
(CHECKLIST: "Webhook signing, verification, replay protection" - the automatic-delivery
half that e2e_webhooks.py deliberately left out of scope, since that script only proves
the on-demand `POST /{id}/test` path).

Nothing here drives the dispatcher. The script creates a real endpoint, causes a real
domain event through the real API, and then only *watches Postgres* - so a PASS means the
deployed `notification-worker` container picked the event out of `outbox_events` on its
own, fanned it out, signed it, and POSTed it to a real public HTTPS destination
(httpbin.org, the same "prove it against something real" standard e2e_webhooks.py already
set). If the dispatch loop were not actually running in that container, every assertion
below would time out rather than quietly pass.

The event is `incident.acknowledged.v1`, emitted by `_transition` in
`backend/tenant_api/app/api/incidents.py` (`event_type=f"incident.{new_status}.v1"`) -
triggered here by a real `POST /api/v1/tenant/incidents/{id}/acknowledge`.

Incidents have no public creation endpoint (they only ever come from the detection
pipeline) - seeded directly via `psql`, exactly as scripts/e2e_exports.py already does for
the same reason. The delivery assertions themselves are read straight out of Postgres:
`webhook_deliveries` is not exposed over any API, the same direct-DB check
scripts/e2e_support_grant_authorization.py established for `audit_events.support_grant_id`.

    python scripts/e2e_webhook_dispatch.py
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "WebhookDispatchE2E!Password123"

# The exact string emitted by tenant_api's incident `_transition` for an acknowledgement.
MATCHING_EVENT = "incident.acknowledged.v1"
# A real event type this run will never produce (the incident is only acknowledged, never
# dismissed), so the second endpoint's filter is genuinely exercised rather than nonsense
# the code could reject for being malformed.
NON_MATCHING_EVENT = "incident.dismissed.v1"

# The worker polls every `webhook_dispatch_poll_seconds` (10s deployed) and each pass does
# fan-out then sending, so a delivery normally lands within two passes. The generous
# ceiling covers a slow httpbin.org round trip and a pass that starts a moment too early.
DELIVERY_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 2

# After the delivery succeeds, sit through more worker passes before counting rows. The
# idempotency assertion is only worth anything if fan-out has had repeated chances to
# re-emit the same event; checking the instant the first delivery lands would pass even
# against a dispatcher with no `processed_events` bookkeeping at all.
IDEMPOTENCY_SETTLE_SECONDS = 25


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


def delivery_row(endpoint_id: str) -> dict:
    """The oldest delivery row for one endpoint, as a dict. '|' is psql -tA's own column
    separator, so `failure_summary_redacted` - the one free-text column here, holding
    whatever a transport error said - is selected last and given the split's remainder,
    rather than being allowed to shift every field after it."""
    raw = psql(
        "SELECT status::text, coalesce(response_status::text, ''), attempt_number, "
        "event_type, coalesce(failure_summary_redacted, '') "
        f"FROM webhook_deliveries WHERE webhook_endpoint_id = '{endpoint_id}' "
        "ORDER BY scheduled_at LIMIT 1"
    )
    if not raw:
        return {}
    status, response_status, attempt, event_type, failure = raw.split("|", 4)
    return {
        "status": status,
        "response_status": int(response_status) if response_status else None,
        "attempt_number": int(attempt),
        "failure": failure,
        "event_type": event_type,
    }


def wait_for_delivery(endpoint_id: str) -> dict:
    """Polls until the worker's own delivery reaches a terminal state, or we give up."""
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    last: dict = {}
    while time.monotonic() < deadline:
        last = delivery_row(endpoint_id)
        if last.get("status") in ("succeeded", "abandoned"):
            return last
        time.sleep(POLL_INTERVAL_SECONDS)
    return last


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Webhook Dispatch E2E {suffix}"
    email = f"owner-{suffix}@northwind.example"

    step(1, "Register a tenant and create a site + camera through the real API")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": organization_name,
        "email": email,
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

    step(2, f"Create a webhook endpoint filtered to exactly '{MATCHING_EVENT}'")
    status, matching = api("/api/v1/tenant/webhooks", {
        "name": "E2E Dispatch Target",
        "url": "https://httpbin.org/post",
        "event_filters": [MATCHING_EVENT],
    }, token, expect=(201,))
    check(status == 201, "the matching endpoint is created", failures)
    check(matching["event_filters"] == [MATCHING_EVENT], "its filter names the real event type", failures)
    matching_id = matching["id"]

    step(3, f"Create a second endpoint filtered to '{NON_MATCHING_EVENT}', which this run never emits")
    # Same destination on purpose: the only difference between the two endpoints is the
    # filter, so a delivery reaching this one could only be a filtering bug.
    status, other = api("/api/v1/tenant/webhooks", {
        "name": "E2E Dispatch Non-Matching",
        "url": "https://httpbin.org/post",
        "event_filters": [NON_MATCHING_EVENT],
    }, token, expect=(201,))
    check(status == 201, "the non-matching endpoint is created", failures)
    other_id = other["id"]

    step(4, "Seed an open incident (no public creation endpoint - see this script's docstring)")
    incident_id = psql(
        "INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, "
        "type_code, severity, status, title, first_detected_at, last_detected_at) VALUES ("
        f"'{tenant_id}', '{site_id}', '{camera_id}', 1, 'zone.intrusion', "
        "'high', 'open', 'Automatic webhook delivery probe', now(), now()) RETURNING id"
    )
    check(bool(incident_id), "an open incident exists to transition", failures)

    step(5, "Acknowledge it through the real API - the only thing this script does to cause a delivery")
    status, transition = api(
        f"/api/v1/tenant/incidents/{incident_id}/acknowledge",
        {"reason": "Automatic webhook delivery e2e"}, token, expect=(200,),
    )
    check(transition.get("status") == "acknowledged", "the incident really transitioned", failures)
    outbox_count = psql(
        f"SELECT count(*) FROM outbox_events WHERE tenant_id = '{tenant_id}' "
        f"AND event_type = '{MATCHING_EVENT}'"
    )
    check(outbox_count == "1", f"exactly one '{MATCHING_EVENT}' row landed in outbox_events", failures)

    step(6, f"Wait for the deployed notification-worker to deliver it (up to {DELIVERY_TIMEOUT_SECONDS}s)")
    print("    nothing below is driven by this script - the container does the work")
    delivered = wait_for_delivery(matching_id)
    check(bool(delivered), "the worker created a delivery row for the matching endpoint", failures)
    check(
        delivered.get("status") == "succeeded",
        f"the delivery reached 'succeeded' (got '{delivered.get('status')}'"
        + (f", reason: {delivered['failure']}" if delivered.get("failure") else "")
        + ")",
        failures,
    )
    check(
        delivered.get("response_status") == 200,
        f"httpbin.org answered 200 (got {delivered.get('response_status')})", failures,
    )
    check(
        delivered.get("event_type") == MATCHING_EVENT,
        f"the delivery carries the real event type (got '{delivered.get('event_type')}')", failures,
    )
    check(
        delivered.get("attempt_number") == 1,
        f"it succeeded on the first attempt (got attempt {delivered.get('attempt_number')})", failures,
    )

    step(7, f"Sit through further worker passes ({IDEMPOTENCY_SETTLE_SECONDS}s) and re-count")
    time.sleep(IDEMPOTENCY_SETTLE_SECONDS)
    matching_total = psql(
        f"SELECT count(*) FROM webhook_deliveries WHERE webhook_endpoint_id = '{matching_id}'"
    )
    check(
        matching_total == "1",
        f"exactly one delivery row exists, not one per pass (got {matching_total}) - "
        "processed_events idempotency holds against the real running worker",
        failures,
    )

    step(8, "The non-matching endpoint received nothing at all")
    other_total = psql(
        f"SELECT count(*) FROM webhook_deliveries WHERE webhook_endpoint_id = '{other_id}'"
    )
    check(other_total == "0", f"zero deliveries for the filtered-out endpoint (got {other_total})", failures)

    processed = psql(
        "SELECT count(*) FROM processed_events p "
        f"JOIN outbox_events e ON e.id = p.event_id AND e.tenant_id = '{tenant_id}' "
        "WHERE p.consumer_name = 'webhook_dispatcher'"
    )
    check(
        processed != "0",
        f"this tenant's outbox events are recorded as consumed by 'webhook_dispatcher' (got {processed})",
        failures,
    )

    step(9, "Clean up")
    # processed_events first, while its rows can still be found by joining outbox_events;
    # then outbox_events, which has no FK to tenants (verified against the live schema) and
    # so would be orphaned rather than cascaded away by the tenant delete below. Endpoints
    # and deliveries do cascade from the tenant.
    psql(
        "DELETE FROM processed_events WHERE consumer_name = 'webhook_dispatcher' "
        f"AND event_id IN (SELECT id FROM outbox_events WHERE tenant_id = '{tenant_id}')"
    )
    psql(f"DELETE FROM outbox_events WHERE tenant_id = '{tenant_id}'")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
    psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
    leftover = psql(
        f"SELECT count(*) FROM webhook_deliveries WHERE webhook_endpoint_id IN ('{matching_id}', '{other_id}')"
    )
    check(leftover == "0", "no delivery rows survive the tenant's deletion", failures)
    print("    test tenant, organization, user, outbox and processed_events rows removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        print("  (check `cd infra && docker compose --env-file ../.env logs notification-worker` "
              "for the worker's own account of what happened)")
        return 1
    print("PASS - a real domain event fanned out of the outbox, the deployed notification-worker "
          "signed and delivered it to a real public endpoint exactly once, and event_filters "
          "kept it away from the endpoint that did not ask for it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
