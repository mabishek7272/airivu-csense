"""Proves the invitation flow works end-to-end for real: invite, accept, log in as the
new member, and that the zero-owners lockout guard actually holds through the real API.

The invitation email is real (Resend is configured in this deployment, CLARIFICATIONS
#26) - rather than needing an inbox to check, this reads the token straight out of Redis,
which `invitation_tickets.py`'s own key format (`cs:{environment}:invitation:{token}`) is
deliberately designed to make easy, the same way other e2e scripts already read Postgres
directly for verification. The real API is never asked to return the token - it only
does that when sending fails, which isn't this run.

    python scripts/e2e_memberships.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "MembershipsE2E!Password123"
MEMBER_PASSWORD = "InvitedMember!Password456"


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


def _redis_password() -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("REDIS_PASSWORD="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("REDIS_PASSWORD not found in .env")


def read_invitation_token() -> str:
    """Reads the (single, freshly-created) invitation key straight out of Redis - the
    real API is never asked to return this, matching how it behaves once an email
    actually sends (see invitation_tickets.py's own docstring for why the key format
    makes this the intended inspection path)."""
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    keys = [k for k in result.stdout.strip().splitlines() if k]
    if len(keys) != 1:
        raise RuntimeError(f"Expected exactly one invitation key, found {len(keys)}: {keys}")
    return keys[0].rsplit(":", 1)[-1]


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register the owner")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Memberships E2E {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Owner",
    })
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    step(2, "Invite a second member for real (a real Resend send is attempted)")
    _, invited = api("/api/v1/tenant/memberships", {
        "email": f"member-{suffix}@northwind.example",
        "display_name": "Invited Member",
        "role_name": "tenant_member",
        "site_scope_mode": "none",
    }, owner_token)
    check(invited["status"] == "invited", "the new membership starts as 'invited'", failures)
    check(
        invited.get("invitation_link") is None,
        "the token is not echoed in the response - it actually sent", failures,
    )

    step(3, "Accept the invitation with the real token, read straight out of Redis")
    token = read_invitation_token()
    status, accepted = api("/api/v1/auth/accept-invitation", {
        "token": token, "password": MEMBER_PASSWORD,
    }, expect=(200,))
    check(status == 200, "accept-invitation issues real tokens (logs the member straight in)", failures)
    check(accepted["tenant_id"] == tenant_id, "the new member lands in the tenant they were invited to", failures)

    step(4, "The accepted member can log in independently too")
    status, logged_in = api("/api/v1/auth/login", {
        "email": f"member-{suffix}@northwind.example", "password": MEMBER_PASSWORD,
    }, expect=(200,))
    check(status == 200, "a fresh login with the chosen password succeeds", failures)
    check(logged_in["tenant_id"] == tenant_id, "login reaches the same tenant", failures)

    step(5, "A second accept attempt with the same token fails - single-use")
    status, _ = api("/api/v1/auth/accept-invitation", {
        "token": token, "password": "SomeOtherPassword123456",
    }, expect=(401,))
    check(status == 401, "the same invitation token cannot be redeemed twice (401)", failures)

    step(6, "The zero-owners lockout guard is real, not just a schema constraint")
    _, memberships = api("/api/v1/tenant/memberships", token=owner_token, method="GET")
    owner_membership = next(m for m in memberships if m["role_name"] == "tenant_owner")
    member_membership = next(m for m in memberships if m["role_name"] == "tenant_member")

    status, body = api(
        f"/api/v1/tenant/memberships/{owner_membership['id']}",
        {"status": "revoked"}, owner_token, method="PATCH", expect=(422,),
    )
    check(status == 422, "revoking the sole owner is refused (422)", failures)
    check(body.get("code") == "last_owner_cannot_be_removed", "the refusal names itself clearly", failures)

    step(7, "Promoting a second owner unblocks it")
    api(
        f"/api/v1/tenant/memberships/{member_membership['id']}",
        {"role_name": "tenant_owner"}, owner_token, method="PATCH", expect=(200,),
    )
    status, _ = api(
        f"/api/v1/tenant/memberships/{owner_membership['id']}",
        {"status": "revoked"}, owner_token, method="PATCH", expect=(200,),
    )
    check(status == 200, "revoking the original owner now succeeds - a second owner exists", failures)

    step(8, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Memberships E2E {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized IN "
         f"('owner-{suffix}@northwind.example', 'member-{suffix}@northwind.example')")
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - invitation, acceptance, and the zero-owners lockout guard all work for real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
