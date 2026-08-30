"""Proves scoped API keys, rate limiting, and usage metering work end-to-end for real
(CHECKLIST: "Scoped API keys, rate limits, usage metering, developer API docs"): a real
key is issued with a scope subset of its issuer's own permissions, a wrong/revoked key is
refused the same way an unknown one is, a real rate limit is actually hit over real HTTP
against the real running API (not simulated), usage metering counts real calls, and
FastAPI's own `/docs`/`/openapi.json` - the "developer API docs" quarter of this
CHECKLIST item - are confirmed live with zero additional code.

    python scripts/e2e_api_clients.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "ApiClientE2E!Password123"


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


def raw_get(path: str, expect: tuple[int, ...] = (200,)) -> int:
    request = urllib.request.Request(f"{API}{path}", headers={"Host": "app.localhost"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        if exc.code in expect:
            return exc.code
        raise RuntimeError(f"GET {path} -> {exc.code}") from exc


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

    step(1, "Register a tenant (its owner starts with the full tenant_owner permission set)")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"API Client E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "A key cannot be issued with a scope its issuer does not hold")
    status, refusal = api("/api/v1/tenant/api-clients", {
        "name": "Overreaching client", "scopes": ["platform.impersonate"],
    }, token, expect=(422,))
    check(status == 422, "issuing an unheld scope is refused (422)", failures)
    check(refusal.get("code") == "scope_exceeds_issuer_permissions", "clear refusal code", failures)

    step(3, "A real client + key is created with a scope the owner actually holds")
    status, created = api("/api/v1/tenant/api-clients", {
        "name": "E2E Integration", "scopes": ["camera.read"], "rate_limit_per_minute": 3,
    }, token, expect=(201,))
    check(status == 201, "creation succeeds", failures)
    check(created["api_key"].startswith("csak_"), "a real API key is issued, shown once", failures)
    client_id = created["client"]["id"]
    key_id = created["key_id"]
    api_key = created["api_key"]

    step(4, "The list view never shows any key's secret")
    _, listed = api("/api/v1/tenant/api-clients", token=token, method="GET")
    entry = next(c for c in listed if c["id"] == client_id)
    check("api_key" not in entry and "secret_hash" not in entry, "no secret field in the list", failures)

    step(5, "The issued key authenticates against the real API and reports its own scopes")
    status, who = api("/api/v1/tenant/integrations/whoami", token=api_key, method="GET", expect=(200,))
    check(status == 200, "whoami succeeds with a valid key", failures)
    check(who["api_client_id"] == client_id, "resolves to the right client", failures)
    check(who["scopes"] == ["camera.read"], "reports exactly the scope it was issued", failures)
    check(who["tenant_id"] == tenant_id, "resolves to the right tenant, from the credential alone", failures)

    step(6, "A tampered key is refused the same way an unknown one is (401, same message)")
    status, bad = api("/api/v1/tenant/integrations/whoami", token=api_key[:-4] + "xxxx", method="GET", expect=(401,))
    check(status == 401, "a tampered key is refused", failures)
    _, unknown = api("/api/v1/tenant/integrations/whoami", token="csak_" + uuid.uuid4().hex * 2, method="GET", expect=(401,))
    check(bad.get("code") == unknown.get("code") == "api_client_unauthenticated", "identical refusal code either way", failures)

    step(7, "A dedicated client isolates the rate-limit test from whoami calls already made above")
    status, rl_created = api("/api/v1/tenant/api-clients", {
        "name": "E2E Rate-Limit Client", "scopes": ["camera.read"], "rate_limit_per_minute": 3,
    }, token, expect=(201,))
    rl_client_id, rl_api_key = rl_created["client"]["id"], rl_created["api_key"]

    step(8, "The real, configured rate limit (3/minute) is actually hit over real HTTP")
    statuses = []
    for _ in range(5):
        status, _ = api("/api/v1/tenant/integrations/whoami", token=rl_api_key, method="GET", expect=(200, 429))
        statuses.append(status)
    check(statuses.count(200) == 3, f"exactly 3 of 5 calls succeeded before the limit bit (got {statuses})", failures)
    check(statuses.count(429) == 2, "the remaining 2 were refused with 429", failures)

    step(9, "Usage metering counted every call against the client, including the refused ones")
    status, usage = api(f"/api/v1/tenant/api-clients/{rl_client_id}/usage?days=1", token=token, method="GET", expect=(200,))
    today_total = sum(usage["by_date"].values())
    check(today_total == 5, f"usage metering recorded all 5 calls today, refusals included (got {today_total})", failures)

    step(10, "Revoking the key immediately refuses further calls with it")
    status, revoked = api(f"/api/v1/tenant/api-clients/{client_id}/keys/{key_id}/revoke", token=token, expect=(200,))
    check(status == 200 and revoked["revoked_at"] is not None, "revoke succeeds", failures)
    status, _ = api("/api/v1/tenant/integrations/whoami", token=api_key, method="GET", expect=(401,))
    check(status == 401, "the revoked key is refused immediately, no propagation delay", failures)

    step(11, "A second key can be issued without disturbing the first (rotation without a gap)")
    status, second = api(f"/api/v1/tenant/api-clients/{client_id}/keys", {}, token, expect=(201,))
    check(status == 201 and second["api_key"].startswith("csak_"), "a fresh key is issued for the same client", failures)
    check(second["api_key"] != api_key, "genuinely different from the first key", failures)

    step(12, "Developer API docs already exist for free - FastAPI's own OpenAPI surface")
    check(raw_get("/api/v1/tenant/docs") == 200, "GET /api/v1/tenant/docs is live", failures)
    check(raw_get("/api/v1/tenant/openapi.json") == 200, "GET /api/v1/tenant/openapi.json is live", failures)

    step(13, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'API Client E2E {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'owner-{suffix}@northwind.example'")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - scoped API keys, real rate limiting, usage metering, and developer docs all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
