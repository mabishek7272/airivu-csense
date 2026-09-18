"""End-to-end verification of the new `membership.read` permission - migration 0057.

Proves against the real running stack that `GET /api/v1/tenant/memberships` (team
roster) is now readable by every customer role, not just tenant_owner (which previously
required `membership.manage`), while write access (`POST .../memberships`, inviting a
new member) stays owner-only.

  1. Register a real tenant (owner gets tenant_owner automatically).
  2. Invite a real member as tenant_viewer through the real POST /api/v1/tenant/memberships.
  3. Read the invitation's Redis-backed token directly and accept it for real through
     POST /api/v1/auth/accept-invitation, exactly like a real invitee would.
  4. Log in as the new tenant_viewer.
  5. Confirm GET /api/v1/tenant/memberships now returns 200 (not 403) for the viewer.
  6. Confirm POST /api/v1/tenant/memberships (invite) still returns 403 for the viewer.

Run from the repo root with the stack up:
    python scripts/e2e_membership_read.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8080"
PASSWORD = "E2EMembershipRead!Password123"


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
        # -q (quiet) is required, not optional: without it psql echoes the SET command's
        # own "SET" tag as an extra output line before the SELECT result.
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
    # `cs:{environment}:invitation:{token}` (this deployment's environment is "local").
    # Scan Redis for the one key referencing this email, then take the token off the
    # end of the key itself.
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


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    org_name = f"Membership Read E2E {suffix}"

    step(1, "Register a real tenant")
    owner_email = f"memread-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": org_name,
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "Invite a tenant_viewer through the real API")
    viewer_email = f"memread-viewer-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": viewer_email, "display_name": "Viewer", "role_name": "tenant_viewer",
    }, owner_token, expect=(201,))
    print(f"    invited viewer={viewer_email}")

    step(3, "Accept the invitation for real, using the real Redis-backed token")
    viewer_ticket = redis_get_invitation_link(viewer_email)
    api("/api/v1/auth/accept-invitation", {"token": viewer_ticket, "password": PASSWORD}, expect=(200,))

    step(4, "Log in as the new tenant_viewer")
    _, viewer_auth = api("/api/v1/auth/login", {"email": viewer_email, "password": PASSWORD})
    viewer_token = viewer_auth["access_token"]

    step(5, "Confirm GET /api/v1/tenant/memberships now returns 200 (not 403) for the viewer")
    status, body = api("/api/v1/tenant/memberships", None, viewer_token, method="GET", expect=(200, 403))
    check(status == 200, f"tenant_viewer's GET /memberships succeeds (got {status})", failures)
    if status == 200:
        emails = {row["email"] for row in body}
        check(owner_email in emails and viewer_email in emails,
              "roster returned includes both the owner and the viewer themselves", failures)

    step(6, "Confirm POST /api/v1/tenant/memberships (invite) still 403s for the viewer")
    status, body = api("/api/v1/tenant/memberships", {
        "email": f"memread-blocked-{suffix}@example.com", "display_name": "Blocked",
        "role_name": "tenant_viewer",
    }, viewer_token, expect=(201, 403))
    check(status == 403, f"tenant_viewer's invite attempt is refused (got {status})", failures)

    step(7, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    for email in (owner_email, viewer_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    # organizations has no ON DELETE CASCADE from tenants - must be deleted explicitly,
    # a real bug already found and fixed twice this session in other e2e scripts.
    psql(f"SET app.is_platform = true; DELETE FROM organizations WHERE display_name = '{org_name}';")
    print("    test tenant, users, and organization removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - membership.read verified for real: tenant_viewer can list the roster, still can't invite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
