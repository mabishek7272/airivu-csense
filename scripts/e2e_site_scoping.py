"""End-to-end verification of per-site membership scoping (migrations 0055, the JWT
ssm/sids claims, and the site-scoped list endpoints). Proves against the real running
stack:

  1. Register a tenant, create two real sites (A, B), one camera, one zone, and one rule
     on each.
  2. Invite a real member scoped to site A only ("selected"), accept the invitation for
     real, log in for real.
  3. Confirm that member's real JWT carries ssm="selected" and sids=[site_a_id] - not by
     inspection of the DB, by decoding the actual token this login call returned.
  4. Confirm the real, enforced HTTP behaviour on all FOUR of the five enforced list
     endpoints that don't need a synthetic detection to populate (sites, cameras, zones,
     rules - see the note below on why `incidents` isn't also driven through HTTP here):
     GET /api/v1/tenant/sites returns only site A; .../cameras, .../zones, .../rules
     each return only site A's own row; GET /api/v1/tenant/sites/{site_b_id} 404s (not
     403 - scoped-out looks like nonexistent).
  5. Confirm the owner (site_scope_mode="all") still sees everything on all four
     endpoints - regression check, this feature must not narrow the owner's own view.
  6. Confirm a 'none'-scoped invite (the new real default) sees zero sites and zero
     cameras.

**Why `incidents` isn't independently driven through HTTP here**: populating a real
incident needs a synthetic detection fed through `ingest_detection()` directly (the
machinery `scripts/e2e_zone_privacy_masking.py` already built for exactly this), which
is disproportionate machinery to duplicate in a script whose actual subject is the
scoping filter itself, not incident creation. That filter is not incident-specific
code, though: `incidents.py list_incidents` calls the exact same
`site_scope_sql_filter()` function (`csense_shared/security/site_scope.py`) that
sites/cameras/zones/rules all call, with the same three-mode behaviour already proven
correct in isolation by `backend/tests/test_site_scope.py`'s 8 unit tests, and
`incidents.py`'s own call site was read and confirmed correct line-by-line during this
feature's own code review (Task 5). Real HTTP coverage on 4 of 5 endpoints plus a
unit-proven, line-reviewed 5th is the deliberate scope of this script - a named
boundary, not a silent gap.

Invitation tokens are read straight out of Redis (`invitation_tickets.py`'s own key
format, `cs:{environment}:invitation:{token}`) rather than needing an inbox to check -
the same pattern `e2e_memberships.py`/`e2e_reseller.py` already use. Unlike those two
scripts, this one does not assume the key it wants is the only one in Redis: a dev
Redis instance commonly carries stale, unexpired invitation keys left over from earlier
e2e runs (invitations live for a week), so `redis_get_invitation_token` scans all
`cs:local:invitation:*` keys and picks the one whose stored JSON payload actually
contains the email being looked up, rather than assuming there's exactly one key.

Run from the repo root with the stack up:
    python scripts/e2e_site_scoping.py
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
PASSWORD = "E2ESiteScoping!Password123"


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


def redis_get_invitation_token(email: str) -> str:
    """Scans every `cs:local:invitation:*` key (there can be several - a dev Redis
    accumulates stale, unexpired invitation keys from prior e2e runs, since invitations
    live a week) and returns the token suffix of whichever key's stored JSON payload
    actually contains this email - not just "the only key found"."""
    password = _redis_password()
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    keys = [k for k in result.stdout.strip().splitlines() if k]
    for key in keys:
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

    step(1, "Register a tenant, create sites A and B, one camera+zone+rule on each")
    owner_email = f"scope-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Site Scoping E2E {suffix}",
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token = auth["access_token"]

    _, site_a = api("/api/v1/tenant/sites", {"name": "Site A", "code": f"a-{suffix}"}, owner_token, expect=(201,))
    _, site_b = api("/api/v1/tenant/sites", {"name": "Site B", "code": f"b-{suffix}"}, owner_token, expect=(201,))
    site_a_id, site_b_id = site_a["id"], site_b["id"]

    _, camera_a = api("/api/v1/tenant/cameras", {
        "site_id": site_a_id, "name": "Cam A", "code": f"cam-a-{suffix}",
    }, owner_token, expect=(201,))
    _, camera_b = api("/api/v1/tenant/cameras", {
        "site_id": site_b_id, "name": "Cam B", "code": f"cam-b-{suffix}",
    }, owner_token, expect=(201,))

    polygon = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    _, zone_a = api("/api/v1/tenant/zones", {
        "site_id": site_a_id, "name": "Zone A", "zone_type": "general",
        "privacy_level": "standard", "polygon": polygon,
    }, owner_token, expect=(201,))
    _, zone_b = api("/api/v1/tenant/zones", {
        "site_id": site_b_id, "name": "Zone B", "zone_type": "general",
        "privacy_level": "standard", "polygon": polygon,
    }, owner_token, expect=(201,))

    _, rule_a = api("/api/v1/tenant/rules", {
        "site_id": site_a_id, "camera_id": camera_a["id"], "zone_id": zone_a["id"],
        "name": "Rule A", "type_code": "zone.intrusion", "alertable_classes": ["person"],
        "min_confidence": 0.5, "min_roi_overlap": 0.1,
    }, owner_token, expect=(201,))
    api("/api/v1/tenant/rules", {
        "site_id": site_b_id, "camera_id": camera_b["id"], "zone_id": zone_b["id"],
        "name": "Rule B", "type_code": "zone.intrusion", "alertable_classes": ["person"],
        "min_confidence": 0.5, "min_roi_overlap": 0.1,
    }, owner_token, expect=(201,))

    step(2, "Invite a member scoped to Site A only, accept and log in for real")
    scoped_email = f"scoped-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": scoped_email, "display_name": "Scoped", "role_name": "tenant_member",
        "site_scope_mode": "selected", "site_ids": [site_a_id],
    }, owner_token, expect=(201,))
    ticket = redis_get_invitation_token(scoped_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket, "password": PASSWORD}, expect=(200,))
    _, scoped_auth = api("/api/v1/auth/login", {"email": scoped_email, "password": PASSWORD})
    scoped_token = scoped_auth["access_token"]

    step(3, "Confirm the real JWT carries ssm=selected and sids=[site_a_id]")
    claims = decode_jwt_claims(scoped_token)
    check(claims.get("ssm") == "selected", f"real token has ssm=selected (got {claims.get('ssm')})", failures)
    check(claims.get("sids") == [site_a_id], f"real token has sids=[site A] (got {claims.get('sids')})", failures)

    step(4, "Confirm real, enforced HTTP behaviour for the scoped member (4 of 5 endpoints)")
    _, sites_seen = api("/api/v1/tenant/sites", token=scoped_token, method="GET", expect=(200,))
    check([s["id"] for s in sites_seen] == [site_a_id], "scoped member's site list contains only Site A", failures)

    _, cameras_seen = api("/api/v1/tenant/cameras", token=scoped_token, method="GET", expect=(200,))
    check(
        [c["id"] for c in cameras_seen] == [camera_a["id"]],
        "scoped member's camera list contains only Site A's camera", failures,
    )

    _, zones_seen = api("/api/v1/tenant/zones", token=scoped_token, method="GET", expect=(200,))
    check(
        [z["id"] for z in zones_seen] == [zone_a["id"]],
        "scoped member's zone list contains only Site A's zone", failures,
    )

    _, rules_seen = api("/api/v1/tenant/rules", token=scoped_token, method="GET", expect=(200,))
    check(
        [r["id"] for r in rules_seen] == [rule_a["id"]],
        "scoped member's rule list contains only Site A's rule", failures,
    )

    status, _ = api(f"/api/v1/tenant/sites/{site_b_id}", token=scoped_token, method="GET", expect=(200, 404))
    check(status == 404, f"scoped member's GET on Site B real-404s, not 403 (got {status})", failures)

    step(5, "Confirm the owner still sees everything on all four endpoints (no regression)")
    _, owner_sites = api("/api/v1/tenant/sites", token=owner_token, method="GET", expect=(200,))
    check(len(owner_sites) == 2, f"owner still sees both sites (got {len(owner_sites)})", failures)
    _, owner_cameras = api("/api/v1/tenant/cameras", token=owner_token, method="GET", expect=(200,))
    check(len(owner_cameras) == 2, f"owner still sees both cameras (got {len(owner_cameras)})", failures)
    _, owner_zones = api("/api/v1/tenant/zones", token=owner_token, method="GET", expect=(200,))
    check(len(owner_zones) == 2, f"owner still sees both zones (got {len(owner_zones)})", failures)
    _, owner_rules = api("/api/v1/tenant/rules", token=owner_token, method="GET", expect=(200,))
    check(len(owner_rules) == 2, f"owner still sees both rules (got {len(owner_rules)})", failures)

    step(6, "Confirm a 'none'-scoped invite (the new real default) sees zero sites and zero cameras")
    none_email = f"none-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": none_email, "display_name": "NoAccess", "role_name": "tenant_member",
    }, owner_token, expect=(201,))
    none_ticket = redis_get_invitation_token(none_email)
    api("/api/v1/auth/accept-invitation", {"token": none_ticket, "password": PASSWORD}, expect=(200,))
    _, none_auth = api("/api/v1/auth/login", {"email": none_email, "password": PASSWORD})
    none_token = none_auth["access_token"]
    _, none_sites = api("/api/v1/tenant/sites", token=none_token, method="GET", expect=(200,))
    check(none_sites == [], f"a fresh 'none'-scoped invite (the real default) sees zero sites (got {len(none_sites)})", failures)
    _, none_cameras = api("/api/v1/tenant/cameras", token=none_token, method="GET", expect=(200,))
    check(none_cameras == [], f"a fresh 'none'-scoped invite sees zero cameras (got {len(none_cameras)})", failures)

    step(7, "Clean up")
    tenant_id = auth["tenant_id"]
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    psql(f"SET app.is_platform = true; DELETE FROM organizations WHERE display_name = 'Site Scoping E2E {suffix}';")
    for email in (owner_email, scoped_email, none_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant, organization, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - site-scoping verified for real: selected/all/none each behave exactly as the JWT claims and HTTP responses show.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
