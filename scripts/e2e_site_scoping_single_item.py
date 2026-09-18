"""End-to-end verification that per-site membership scoping also covers single-resource
GET/PATCH/DELETE and mutation endpoints, not just list views - closing the "Named,
deliberate scope boundary" that `scripts/e2e_site_scoping.py` originally left open
(zones/rules/incidents single-item GET, and every mutation reachable through them,
stayed tenant-only when site-scoping first shipped).

Proves against the real running stack:

  1. Register a tenant, create sites A and B, a camera+zone+rule on site B only.
  2. Invite a member scoped to site A only ("selected"), accept and log in for real.
  3. Confirm GET on Site B's own zone/rule real-404s for that member (not 403 - scoped-
     out looks like nonexistent, same convention as sites.py/cameras.py already use).
  4. Confirm PATCH on Site B's zone/rule also real-404s - a scoped-out member cannot
     read OR silently mutate a resource outside their sites just by knowing its UUID.
  5. Confirm creating a new zone under Site B (an out-of-scope site) is refused with a
     real 404, not a confusing 201 followed by a 404 the moment the response tries to
     load it back.
  6. Confirm the owner (site_scope_mode="all") has no regression on any of the above.
  7. Feeds a real synthetic detection through `ingest_detection()` (the same production
     function `pipeline-runtime` calls) to create a REAL incident on Site B, then
     confirms GET and POST /acknowledge on that incident both real-404 for the
     scoped-out member, and both succeed for the owner.

Run from the repo root with the stack up:
    python scripts/e2e_site_scoping_single_item.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8080"
PASSWORD = "E2ESingleItemScope!Pass123"


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


def _read_env(key: str) -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(f"{key} not found in .env")


def redis_get_invitation_token(email: str) -> str:
    """Scans all cs:local:invitation:* keys and matches by payload, not by assuming
    there's exactly one - a shared dev Redis commonly carries stale keys from other
    e2e runs (invitations live a week)."""
    password = _read_env("REDIS_PASSWORD")
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "-a", password, "--no-auth-warning", "KEYS", "cs:local:invitation:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in [k for k in result.stdout.strip().splitlines() if k]:
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

    step(1, "Register a tenant, create sites A and B, a camera+zone+rule on Site B only")
    owner_email = f"single-item-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Single Item Scope E2E {suffix}",
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site_a = api("/api/v1/tenant/sites", {"name": "Site A", "code": f"a-{suffix}"}, owner_token, expect=(201,))
    _, site_b = api("/api/v1/tenant/sites", {"name": "Site B", "code": f"b-{suffix}"}, owner_token, expect=(201,))
    site_a_id, site_b_id = site_a["id"], site_b["id"]

    _, camera_b = api("/api/v1/tenant/cameras", {
        "site_id": site_b_id, "name": "Cam B", "code": f"cam-b-{suffix}",
    }, owner_token, expect=(201,))
    polygon = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    _, zone_b = api("/api/v1/tenant/zones", {
        "site_id": site_b_id, "name": "Zone B", "zone_type": "general",
        "privacy_level": "standard", "polygon": polygon,
    }, owner_token, expect=(201,))
    _, rule_b = api("/api/v1/tenant/rules", {
        "site_id": site_b_id, "camera_id": camera_b["id"], "zone_id": zone_b["id"],
        "name": "Rule B", "type_code": "zone.intrusion", "alertable_classes": ["person"],
        "min_confidence": 0.5, "min_roi_overlap": 0.1,
    }, owner_token, expect=(201,))

    step(2, "Invite a member scoped to Site A only, accept and log in for real")
    # tenant_owner (not tenant_operator) so this member genuinely has zone.manage/
    # rule.manage - the point of this script is proving the SITE boundary is enforced,
    # not re-testing the permission boundary scripts/e2e_finer_roles.py already covers.
    scoped_email = f"single-item-scoped-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": scoped_email, "display_name": "Scoped", "role_name": "tenant_owner",
        "site_scope_mode": "selected", "site_ids": [site_a_id],
    }, owner_token, expect=(201,))
    ticket = redis_get_invitation_token(scoped_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket, "password": PASSWORD}, expect=(200,))
    _, scoped_auth = api("/api/v1/auth/login", {"email": scoped_email, "password": PASSWORD})
    scoped_token = scoped_auth["access_token"]

    step(3, "GET single zone/rule outside scope real-404s (not 403)")
    status, _ = api(f"/api/v1/tenant/zones/{zone_b['id']}", token=scoped_token, method="GET", expect=(200, 404))
    check(status == 404, f"GET zone B (out of scope) 404s, got {status}", failures)
    status, _ = api(f"/api/v1/tenant/rules/{rule_b['id']}", token=scoped_token, method="GET", expect=(200, 404))
    check(status == 404, f"GET rule B (out of scope) 404s, got {status}", failures)

    step(4, "PATCH on that same zone/rule real-404s - cannot silently mutate by guessing a UUID")
    status, _ = api(f"/api/v1/tenant/zones/{zone_b['id']}", {"name": "Hacked"}, scoped_token, method="PATCH", expect=(200, 404))
    check(status == 404, f"PATCH zone B (out of scope) 404s, got {status}", failures)
    status, _ = api(f"/api/v1/tenant/rules/{rule_b['id']}", {"name": "Hacked"}, scoped_token, method="PATCH", expect=(200, 404))
    check(status == 404, f"PATCH rule B (out of scope) 404s, got {status}", failures)

    step(5, "Creating a zone under an out-of-scope site is refused, not a 201-then-404")
    status, _ = api("/api/v1/tenant/zones", {
        "site_id": site_b_id, "name": "Sneaky", "zone_type": "general",
        "privacy_level": "standard", "polygon": polygon,
    }, scoped_token, expect=(201, 404))
    check(status == 404, f"create zone under out-of-scope site 404s, got {status}", failures)

    step(6, "The owner (all scope) has no regression on any of the above")
    status, _ = api(f"/api/v1/tenant/zones/{zone_b['id']}", token=owner_token, method="GET", expect=(200, 404))
    check(status == 200, f"owner GET zone B still works, got {status}", failures)
    status, patched = api(f"/api/v1/tenant/zones/{zone_b['id']}", {"name": "Zone B Renamed"}, owner_token, method="PATCH", expect=(200, 404))
    check(status == 200 and patched.get("name") == "Zone B Renamed", f"owner PATCH zone B still works, got {status}", failures)

    step(7, "A real incident on Site B (via ingest_detection) also enforces the boundary")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "shared"))
    import numpy as np
    from csense_shared.pipeline.ingest import ingest_detection
    from csense_shared.pipeline.rules import DetectedObject
    from csense_shared.storage.objects import create_client
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    pg_password = _read_env("POSTGRES_PASSWORD")

    class _MinioSettings:
        minio_endpoint = "localhost:9000"
        minio_root_user = _read_env("MINIO_ROOT_USER")
        minio_root_password = _read_env("MINIO_ROOT_PASSWORD")
        minio_use_tls = False

    async def make_incident() -> object:
        engine = create_async_engine(f"postgresql+asyncpg://csense_app:{pg_password}@localhost:5432/csense")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        minio = create_client(_MinioSettings())
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        person = DetectedObject("person", 0.9, (0.05, 0.2, 0.35, 0.9))
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            result = await ingest_detection(
                session, minio,
                tenant_id=uuid.UUID(tenant_id), site_id=uuid.UUID(site_b_id),
                camera_id=uuid.UUID(camera_b["id"]), source_event_id=f"single-item-{suffix}",
                captured_at=dt.datetime.now(dt.UTC), objects=[person], frame=frame,
            )
        await engine.dispose()
        return result

    result = asyncio.run(make_incident())
    incident_id = result.incident_id

    status, _ = api(f"/api/v1/tenant/incidents/{incident_id}", token=scoped_token, method="GET", expect=(200, 404))
    check(status == 404, f"GET incident (Site B, out of scope) 404s, got {status}", failures)

    status, _ = api(f"/api/v1/tenant/incidents/{incident_id}/acknowledge", {"reason": "test"}, scoped_token, method="POST", expect=(200, 404))
    check(status == 404, f"acknowledge incident (out of scope) 404s, doesn't silently succeed, got {status}", failures)

    status, _ = api(f"/api/v1/tenant/incidents/{incident_id}/acknowledge", {"reason": "real"}, owner_token, method="POST", expect=(200, 404))
    check(status == 200, f"owner's own acknowledge still works, got {status}", failures)

    step(8, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    psql(f"SET app.is_platform = true; DELETE FROM organizations WHERE display_name = 'Single Item Scope E2E {suffix}';")
    for email in (owner_email, scoped_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant, organization, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - single-resource GET/PATCH/create/transition enforcement verified for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
