"""End-to-end verification of the two new fixed roles (tenant_viewer, tenant_operator) -
migration 0054. Proves against the real running stack:

  1. Register a real tenant (owner gets tenant_owner automatically).
  2. Invite a real member as tenant_viewer and one as tenant_operator through the real
     POST /api/v1/tenant/memberships.
  3. Read each invitation's Redis-backed token directly and accept it for real through
     POST /api/v1/auth/accept-invitation, exactly like a real invitee would.
  4. Log in as each new user and inspect their real JWT's "perm" claim - confirm
     tenant_viewer has read-only permissions and lacks incident.acknowledge/camera.manage,
     confirm tenant_operator has camera.manage/rule.manage/incident.acknowledge and lacks
     membership.manage/tenant.settings.manage.
  5. Confirm the real permission boundary at the HTTP layer, not just the claim: the
     viewer's token gets a real 403 from POST /api/v1/tenant/cameras (camera.manage
     required); the operator's token succeeds creating a real camera.

Run from the repo root with the stack up:
    python scripts/e2e_finer_roles.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8080"
PASSWORD = "E2EFinerRoles!Password123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{BASE}{path}",
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
        # -q (quiet) is required, not optional: this helper is called with multi-
        # statement SQL ("SET app.is_platform = true; SELECT ...") and without -q psql
        # echoes the SET command's own "SET" tag as an extra output line before the
        # SELECT result, which would otherwise become lines[0] instead of the real value.
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-q", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _redis_password() -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("REDIS_PASSWORD="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("REDIS_PASSWORD not found in .env")


def redis_get_invitation_link(email: str) -> str:
    # Invitation tokens live in Redis under invitation_tickets.py's real key format
    # `cs:{environment}:invitation:{token}` (this deployment's environment is "local",
    # per .env's ENVIRONMENT=local) - not a generic "invite:*" pattern. The server
    # requires auth (REDIS_PASSWORD in .env), so redis-cli needs `-a`. The simplest real
    # way to get the token back out for a script (not a test double) is the same one
    # scripts/e2e_memberships.py already uses: scan Redis for the one key referencing
    # this email, then take the token off the end of the key itself.
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in result.stdout.strip().splitlines():
        if not key:
            continue
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
             "redis-cli", "-a", password, "--no-auth-warning", "GET", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.rsplit(":", 1)[-1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def decode_jwt_claims(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a real tenant")
    owner_email = f"roles-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Finer Roles E2E {suffix}",
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}",
    }, owner_token, expect=(201,))
    site_id = site["id"]

    step(2, "Invite one tenant_viewer and one tenant_operator through the real API")
    viewer_email = f"viewer-{suffix}@example.com"
    operator_email = f"operator-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": viewer_email, "display_name": "Viewer", "role_name": "tenant_viewer",
    }, owner_token, expect=(201,))
    api("/api/v1/tenant/memberships", {
        "email": operator_email, "display_name": "Operator", "role_name": "tenant_operator",
    }, owner_token, expect=(201,))
    print(f"    invited viewer={viewer_email} operator={operator_email}")

    step(3, "Accept both invitations for real, using the real Redis-backed token")
    viewer_ticket = redis_get_invitation_link(viewer_email)
    operator_ticket = redis_get_invitation_link(operator_email)
    api("/api/v1/auth/accept-invitation", {"token": viewer_ticket, "password": PASSWORD}, expect=(200,))
    api("/api/v1/auth/accept-invitation", {"token": operator_ticket, "password": PASSWORD}, expect=(200,))

    step(4, "Log in as each and inspect the real JWT perm claim")
    _, viewer_auth = api("/api/v1/auth/login", {"email": viewer_email, "password": PASSWORD})
    _, operator_auth = api("/api/v1/auth/login", {"email": operator_email, "password": PASSWORD})
    viewer_token = viewer_auth["access_token"]
    operator_token = operator_auth["access_token"]

    viewer_perms = set(decode_jwt_claims(viewer_token)["perm"])
    operator_perms = set(decode_jwt_claims(operator_token)["perm"])

    check("incident.read" in viewer_perms and "camera.read" in viewer_perms,
          "tenant_viewer's real token carries read permissions", failures)
    check("incident.acknowledge" not in viewer_perms and "camera.manage" not in viewer_perms,
          "tenant_viewer's real token has no operational permissions", failures)
    check("camera.manage" in operator_perms and "rule.manage" in operator_perms
          and "incident.acknowledge" in operator_perms,
          "tenant_operator's real token carries operational permissions", failures)
    check("membership.manage" not in operator_perms and "tenant.settings.manage" not in operator_perms,
          "tenant_operator's real token has no user/settings-management permissions", failures)

    step(5, "Confirm the real 403/201 boundary at the HTTP layer, not just the claim")
    status, body = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Should Fail", "code": f"viewer-cam-{suffix}",
    }, viewer_token, expect=(201, 403))
    check(status == 403, f"tenant_viewer's real camera-create call is refused (got {status})", failures)

    status, body = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Operator Cam", "code": f"operator-cam-{suffix}",
    }, operator_token, expect=(201, 403))
    check(status == 201, f"tenant_operator's real camera-create call succeeds (got {status})", failures)

    step(6, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    for email in (owner_email, viewer_email, operator_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - tenant_viewer and tenant_operator verified for real, at both the JWT and HTTP layers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
