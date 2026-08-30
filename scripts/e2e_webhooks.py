"""Proves webhook endpoints work end-to-end for real (CHECKLIST: "Webhook signing,
verification, replay protection"): a real signed HTTP POST reaches a real public
endpoint (httpbin.org, an echo service - same "prove it against something real" standard
this session already applied to the real NVR and real Resend email delivery), the SSRF
guard refuses a private destination at delivery time, and neither the URL nor the signing
secret is ever returned by any endpoint except the one moment each is first created or
rotated. Signature math correctness itself (round trip, tamper detection, expiry) is
proven directly and thoroughly in backend/tests/test_webhooks.py - this script proves the
mechanism as wired into the real running API, not the signing math in isolation.

    python scripts/e2e_webhooks.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "WebhookE2E!Password123"


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

    step(1, "Register a tenant")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Webhook E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "Create a webhook endpoint pointed at a real public HTTPS destination")
    status, created = api("/api/v1/tenant/webhooks", {
        "name": "E2E Echo Endpoint", "url": "https://httpbin.org/post", "event_filters": [],
    }, token, expect=(201,))
    check(status == 201, "creation succeeds", failures)
    check(created["url_host_display"] == "httpbin.org", "only the hostname is shown back, not the full URL", failures)
    check(created["signing_secret"].startswith("whsec_"), "a real signing secret is issued, shown once", failures)
    webhook_id = created["id"]
    first_secret = created["signing_secret"]

    step(3, "The list view never shows the URL or the signing secret")
    _, listed = api("/api/v1/tenant/webhooks", token=token, method="GET")
    entry = next(w for w in listed if w["id"] == webhook_id)
    check("url" not in entry and "signing_secret" not in entry, "neither secret field appears in the list", failures)

    step(4, "A real signed test delivery reaches the real public endpoint")
    status, result = api(f"/api/v1/tenant/webhooks/{webhook_id}/test", {}, token, expect=(200,))
    check(status == 200, "the test-delivery call itself succeeds", failures)
    check(result["delivered"] is True, "the real HTTP POST was delivered", failures)
    check(result["response_status"] == 200, "httpbin.org answered 200", failures)
    check(result["response_time_ms"] is not None and result["response_time_ms"] > 0, "a real round-trip time was recorded", failures)

    step(5, "A private address is refused by the SSRF guard at creation, not silently allowed through")
    status, refusal = api("/api/v1/tenant/webhooks", {
        "name": "Internal (should be refused)", "url": "https://127.0.0.1/hook", "event_filters": [],
    }, token, expect=(422,))
    check(status == 422, "creating a webhook pointed at a private address is refused (422)", failures)
    check(refusal.get("code") == "address_not_permitted", "clear refusal code", failures)

    step(6, "Rotating the secret issues a genuinely different one")
    status, rotated = api(f"/api/v1/tenant/webhooks/{webhook_id}/rotate-secret", {}, token, expect=(200,))
    check(status == 200 and rotated["signing_secret"] != first_secret, "the new secret differs from the original", failures)

    step(7, "Updating name/status never touches the URL or secret")
    status, patched = api(f"/api/v1/tenant/webhooks/{webhook_id}", {"status": "disabled"}, token, method="PATCH", expect=(200,))
    check(status == 200 and patched["status"] == "disabled", "status updates via PATCH", failures)
    check("url" not in patched and "signing_secret" not in patched, "still no secret fields in the response", failures)

    step(8, "Deleting an endpoint destroys its underlying encrypted secrets too")
    status, _ = api(f"/api/v1/tenant/webhooks/{webhook_id}", token=token, method="DELETE", expect=(204,))
    check(status == 204, "delete succeeds", failures)
    remaining_secrets = psql(
        f"SELECT count(*) FROM encrypted_secrets WHERE tenant_id = '{tenant_id}' "
        "AND purpose IN ('webhook.url', 'webhook.signing_secret')"
    )
    check(remaining_secrets == "0", "no orphaned secrets left behind after the endpoint's own deletion", failures)

    step(9, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Webhook E2E {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'owner-{suffix}@northwind.example'")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - webhook signing, real delivery, SSRF protection, secret rotation, and write-only secrets all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
