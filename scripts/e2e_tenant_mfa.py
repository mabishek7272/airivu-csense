"""Proves the tenant-side TOTP MFA mirror of scripts/e2e_mfa.py works for real, on a
real registered tenant rather than a platform admin: enroll, confirm with a computed
code, verify (TOTP and recovery code), single-use recovery codes, and remove-requires-
step-up. This is the "MFA enrollment" step of the Phase 2 vertical-slice test
(reseller -> child tenant -> MFA enrollment -> empty dashboard -> quota-exceeded
rejection) exercised directly against a tenant owner, independent of the admin-side flow.

    python scripts/e2e_tenant_mfa.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

from csense_shared.security.totp import totp_now

API = "http://localhost:8080"
OWNER_PASSWORD = "TenantMfaE2E!Password123"


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

    step(1, "Register a tenant owner, not yet MFA-enrolled")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Tenant MFA E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD,
        "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, status0 = api("/api/v1/tenant/auth/mfa/status", token=token, method="GET")
    check(status0 == {"enrolled": False, "recovery_codes_remaining": 0}, "starts unenrolled", failures)

    step(2, "Enroll a real TOTP secret and confirm it with a real computed code")
    _, enrolled = api("/api/v1/tenant/auth/mfa/totp/enroll", token=token, expect=(201,))
    secret = enrolled["secret"]
    check(enrolled["otpauth_uri"].startswith("otpauth://totp/"), "a real otpauth:// URI is returned", failures)

    code = totp_now(secret)
    status, confirmed = api("/api/v1/tenant/auth/mfa/totp/confirm", {"code": code}, token, expect=(200,))
    check(status == 200, "confirming with the real computed code succeeds", failures)
    recovery_codes = confirmed["recovery_codes"]
    check(len(recovery_codes) == 10, "10 recovery codes are issued", failures)

    _, status1 = api("/api/v1/tenant/auth/mfa/status", token=token, method="GET")
    check(status1 == {"enrolled": True, "recovery_codes_remaining": 10}, "status now shows enrolled", failures)

    step(3, "Removing MFA before any step-up is refused")
    status, refusal = api("/api/v1/tenant/auth/mfa/totp", token=token, method="DELETE", expect=(403,))
    check(status == 403, "DELETE without a recent verification is refused (403)", failures)
    check(refusal.get("code") == "step_up_required", "clear refusal code", failures)

    step(4, "A wrong code is refused")
    status, _ = api("/api/v1/tenant/auth/mfa/verify", {"code": "000000"}, token, expect=(401,))
    check(status == 401, "an incorrect TOTP code is refused (401)", failures)

    step(5, "A correct code verifies")
    code = totp_now(secret)
    status, verified = api("/api/v1/tenant/auth/mfa/verify", {"code": code}, token, expect=(200,))
    check(status == 200 and verified["verified"] and not verified["used_recovery_code"], "verifies via TOTP", failures)

    step(6, "A recovery code verifies too, and is single-use")
    recovery_code = recovery_codes[0]
    status, verified = api("/api/v1/tenant/auth/mfa/verify", {"recovery_code": recovery_code}, token, expect=(200,))
    check(status == 200 and verified["used_recovery_code"], "the recovery code verifies", failures)

    status, _ = api("/api/v1/tenant/auth/mfa/verify", {"recovery_code": recovery_code}, token, expect=(401,))
    check(status == 401, "the same recovery code cannot be reused (401)", failures)

    _, status2 = api("/api/v1/tenant/auth/mfa/status", token=token, method="GET")
    check(status2["recovery_codes_remaining"] == 9, "exactly one recovery code was consumed", failures)

    step(7, "This tenant owner's own MFA is isolated from a second, separate tenant owner")
    _, second_auth = api("/api/v1/auth/register", {
        "organization_name": f"Tenant MFA E2E Other {suffix}",
        "email": f"other-owner-{suffix}@northwind.example",
        "password": OWNER_PASSWORD,
        "display_name": "Other Owner",
    })
    second_token, second_tenant_id = second_auth["access_token"], second_auth["tenant_id"]
    _, second_status = api("/api/v1/tenant/auth/mfa/status", token=second_token, method="GET")
    check(
        second_status == {"enrolled": False, "recovery_codes_remaining": 0},
        "a separate tenant owner is unaffected - not enrolled, no shared state", failures,
    )

    step(8, "With a fresh step-up, removing MFA now succeeds")
    status, _ = api("/api/v1/tenant/auth/mfa/totp", token=token, method="DELETE", expect=(204,))
    check(status == 204, "DELETE succeeds once a recent step-up exists", failures)

    _, status3 = api("/api/v1/tenant/auth/mfa/status", token=token, method="GET")
    check(status3 == {"enrolled": False, "recovery_codes_remaining": 0}, "fully unenrolled", failures)

    step(9, "Clean up")
    psql(f"DELETE FROM tenants WHERE id IN ('{tenant_id}', '{second_tenant_id}')")
    psql(f"DELETE FROM organizations WHERE display_name IN "
         f"('Tenant MFA E2E {suffix}', 'Tenant MFA E2E Other {suffix}')")
    psql(
        "DELETE FROM users WHERE email_normalized IN ("
        f"'owner-{suffix}@northwind.example', 'other-owner-{suffix}@northwind.example')"
    )
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - tenant-side TOTP enrollment, recovery codes, and step-up all work for real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
