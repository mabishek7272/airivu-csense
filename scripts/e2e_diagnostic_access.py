"""Proves Task 3 of `docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-
desired-state.md`: `GET /api/v1/tenant/edge/devices/{id}/diagnostics` (Task 2, gated by the
new `diagnostic.read` permission from migration 0053) is reachable both by an ordinary
tenant user *and* by a platform developer elevated through a real, peer-approved support
grant (`scripts/e2e_support_grant_authorization.py`'s own mechanism, a second real
consumer of it) - and that the elevation this grant provides is genuinely read-only, not
merely intended to be.

Runs against a real `edge-agent` container (`scripts/e2e_edge_spool.py`'s container-start/
enrol/teardown pattern, reused rather than re-derived) so the log tail asserted on is the
agent's own real `logbuf.py` ring buffer, not a fixture.

Covers, in order:
  1. A real enrolled device heartbeats with a real, content-bearing `health.logs` tail
     (confirmed via `.../diagnostics`, not just "present").
  2. The tenant's own owner login reads `.../diagnostics` directly - 200, real content.
  3. A platform developer with no active grant gets 401 on the same route.
  4. A support grant requesting only `["diagnostic.read"]` is peer-approved (mirrors
     `e2e_support_grant_authorization.py`'s two-developer bootstrap/approve pattern); the
     elevated developer now reads the *same* device's diagnostics through the *same*
     route - 200, content matching what the tenant owner saw.
  5. The single most important assertion: with that grant still active, the elevated
     developer attempts an unrelated tenant-scoped write - issuing a device command,
     which needs `edge.manage` - and is refused. The grant's `requested_scopes` never
     included anything but `diagnostic.read`, so this is what proves "read-only" is an
     enforced property of `require_permission`, not just this session's stated intent.
  6. The diagnostics response, even under elevation, never carries a command's `payload`/
     `signed_envelope` - Task 2's response-narrowing (`DiagnosticCommandOut`) holds under
     the real elevation path too, not only for an ordinary tenant user.
  7. The grant is revoked; the identical elevated call now gets 401 immediately.
  8. Full cleanup: container, tenant/org/users/device, support grant row.

    python scripts/e2e_diagnostic_access.py
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password

API = "http://localhost:8080"
OWNER_PASSWORD = "DiagAccessE2E!Owner123"
PLATFORM_ADMIN_PASSWORD = "DiagAccessE2E!Platform123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204), host="app.localhost", extra_headers=None):
    headers = {"Content-Type": "application/json", "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
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
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:500]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:800]}"
        )
    return result


def compose_project_and_network() -> tuple[str, str]:
    """Mirrors `e2e_edge_spool.py`'s own helper - discovers the real compose project/
    network from the already-running `tenant-api` container rather than hardcoding it."""
    container_id = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "ps", "-q", "tenant-api"],
        cwd="infra", capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not container_id:
        raise RuntimeError("tenant-api is not running - start the stack first (docker compose up -d).")
    project = docker(
        "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', container_id
    ).stdout.decode().strip()
    networks = json.loads(
        docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id).stdout
    )
    network = next(iter(networks))
    return project, network


def build_edge_agent_image() -> None:
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "build", "edge-agent"],
        cwd="infra", check=True,
    )


def wait_for_device_online(token: str, device_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    device: dict = {}
    while time.monotonic() < deadline:
        _, device = api(f"/api/v1/tenant/edge/devices/{device_id}", token=token, method="GET")
        if device.get("status") == "online" and device.get("online"):
            return device
        time.sleep(1.0)
    return device


def wait_for_log_tail(token: str, device_id: str, *, timeout: float = 30.0) -> dict:
    """Polls `.../diagnostics` until `health.logs` is a genuinely non-empty, content-
    bearing list - not merely present as an empty key. The agent logs real events
    (enrolment, heartbeat) from process start, so this should resolve well within one or
    two heartbeat cycles, not require any synthetic trigger."""
    deadline = time.monotonic() + timeout
    diagnostics: dict = {}
    while time.monotonic() < deadline:
        _, diagnostics = api(
            f"/api/v1/tenant/edge/devices/{device_id}/diagnostics", token=token, method="GET",
        )
        logs = (diagnostics.get("health") or {}).get("logs")
        if isinstance(logs, list) and len(logs) > 0:
            return diagnostics
        time.sleep(1.0)
    return diagnostics


def bootstrap_platform_admin(label: str) -> tuple[str, str, str]:
    """Mirrors `e2e_support_grant_authorization.py`'s own helper. Returns (email, user_id,
    token)."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"diag-access-e2e-{label}-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', %s) RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings), f"Diag Access E2E {label}"),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
        conn.commit()
    _, auth = api("/api/v1/admin/auth/login", {"email": email, "password": PLATFORM_ADMIN_PASSWORD}, host="console.localhost")
    return email, str(user_id), auth["access_token"]


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def elevated_diagnostics_call(token: str, device_id: str, tenant_id_header: str | None, expect=(200, 401)):
    headers = {"X-CSense-Support-Tenant-Id": tenant_id_header} if tenant_id_header else {}
    return api(
        f"/api/v1/tenant/edge/devices/{device_id}/diagnostics", token=token, method="GET",
        extra_headers=headers, expect=expect,
    )


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Diag Access E2E {suffix}"
    email = f"owner-{suffix}@northwind.example"
    container_name = f"csense-diag-access-e2e-{suffix}"
    volume_name = f"csense-diag-access-e2e-{suffix}"

    tenant_id = None
    dev_a_email = dev_b_email = None
    container_started = False
    volume_created = False
    project = network = image = None

    try:
        step(0, "Discover the real compose project/network and build the agent image")
        project, network = compose_project_and_network()
        image = f"{project}-edge-agent:latest"
        print(f"    project={project} network={network} image={image}")
        build_edge_agent_image()
        check(bool(docker("image", "inspect", image, check=False).returncode == 0), f"image '{image}' exists after build", failures)

        step(1, "Register a tenant and create a site through the real API")
        _, auth = api("/api/v1/auth/register", {
            "organization_name": organization_name,
            "email": email,
            "password": OWNER_PASSWORD, "display_name": "Owner",
        })
        owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

        _, site = api("/api/v1/tenant/sites", {
            "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
        }, owner_token, expect=(201,))
        site_id = site["id"]

        step(2, "Create a device record and issue a real, single-use enrolment token")
        status, device = api("/api/v1/tenant/edge/devices", {
            "name": "E2E Diagnostics Agent", "site_id": site_id,
            "device_type": "pc_linux", "role": "inference",
        }, owner_token, expect=(201,))
        check(status == 201, "device record created", failures)
        device_id = device["id"]

        status, enrolment = api(
            f"/api/v1/tenant/edge/devices/{device_id}/enrolment-token", None, owner_token, expect=(201,)
        )
        check(status == 201, "enrolment token issued", failures)
        enrolment_token = enrolment["token"]

        step(3, "Start a real edge-agent container, pointed at the real token")
        docker("volume", "create", volume_name)
        volume_created = True
        docker(
            "run", "-d", "--name", container_name, "--network", network,
            "-e", "CSENSE_API_BASE_URL=http://tenant-api:8000",
            "-e", "CSENSE_STATE_DIR=/var/lib/csense-agent",
            "-e", f"CSENSE_ENROLMENT_TOKEN={enrolment_token}",
            "-e", f"CSENSE_SERIAL_NUMBER=e2e-diag-{suffix}",
            "-v", f"{volume_name}:/var/lib/csense-agent",
            "--tmpfs", "/tmp",
            "--read-only",
            "--security-opt", "no-new-privileges:true",
            image,
        )
        container_started = True

        step(4, "The agent enrols for real and the device reaches 'online'")
        device_after_enrol = wait_for_device_online(owner_token, device_id)
        check(device_after_enrol.get("status") == "online", "device status is 'online'", failures)
        check(device_after_enrol.get("online") is True, "device's derived 'online' flag is true", failures)

        step(5, "The owner (who genuinely holds edge.manage) issues a real signed command, so command history below is non-empty")
        status, issued_command = api(
            f"/api/v1/tenant/edge/devices/{device_id}/commands",
            {"command_type": "diagnostic.ping", "payload": {"probe": True}, "idempotency_key": f"e2e-diag-owner-{suffix}"},
            token=owner_token, expect=(201,),
        )
        check(status == 201, "owner successfully issues a real device command", failures)
        check(
            "payload" in issued_command and "signed_envelope" in issued_command,
            "the ordinary command-issue response (CommandOut) does carry payload/signed_envelope - "
            "confirms DiagnosticsOut's narrowing below is a deliberate omission, not an accident of an empty field",
            failures,
        )

        step(6, "The owner reads .../diagnostics directly: 200, real content-bearing health.logs tail")
        owner_diagnostics = wait_for_log_tail(owner_token, device_id, timeout=40.0)
        owner_logs = ((owner_diagnostics.get("health") or {}).get("logs")) or []
        check(len(owner_logs) > 0, f"health.logs is non-empty ({len(owner_logs)} entries)", failures)
        first_entry = owner_logs[0] if owner_logs else {}
        check(
            isinstance(first_entry, dict) and {"timestamp", "level", "message"} <= set(first_entry),
            f"a log entry is structured (timestamp/level/message), not a raw blob (got keys {sorted(first_entry) if isinstance(first_entry, dict) else first_entry!r})",
            failures,
        )
        check(
            any(isinstance(e, dict) and e.get("message") for e in owner_logs),
            "at least one log entry carries a real, non-empty message", failures,
        )
        check("health_status" in owner_diagnostics, "diagnostics response carries health_status", failures)
        check(owner_diagnostics.get("device_id") == device_id, "diagnostics response is for the right device", failures)
        owner_commands = owner_diagnostics.get("commands") or []
        check(
            any(c.get("command_type") == "diagnostic.ping" for c in owner_commands),
            "the command issued in step 5 shows up in the owner's own command history", failures,
        )

        step(7, "A platform developer with no active grant gets 401 on the same route")
        dev_a_email, _dev_a_id, dev_a_token = bootstrap_platform_admin("a")
        dev_b_email, _dev_b_id, dev_b_token = bootstrap_platform_admin("b")
        status, _ = elevated_diagnostics_call(dev_a_token, device_id, tenant_id, expect=(401,))
        check(status == 401, "no active grant yet -> 401", failures)

        step(8, "Developer A requests a support grant naming only diagnostic.read")
        status, requested = api(
            "/api/v1/admin/support-grants",
            {
                "tenant_id": tenant_id, "ticket_reference": f"TICKET-{suffix}",
                "purpose": "Investigating a customer-reported device connectivity issue.",
                "requested_scopes": ["diagnostic.read"], "ttl_hours": 8,
            },
            dev_a_token, host="console.localhost", expect=(201,),
        )
        check(status == 201 and requested["status"] == "requested", "grant created, awaiting approval", failures)
        grant_id = requested["id"]

        step(9, "Developer B, a real peer, approves it - and it becomes active")
        status, approved = api(
            f"/api/v1/admin/support-grants/{grant_id}/approve", token=dev_b_token, host="console.localhost", expect=(200,),
        )
        check(status == 200 and approved["status"] == "active", "approved by a peer -> active", failures)

        step(10, "The elevated developer now reads the SAME device's diagnostics through the SAME route: 200, matching content")
        status, elevated_diagnostics = elevated_diagnostics_call(dev_a_token, device_id, tenant_id, expect=(200,))
        check(status == 200, "elevated diagnostics call succeeds (200)", failures)
        elevated_logs = ((elevated_diagnostics.get("health") or {}).get("logs")) or []
        check(len(elevated_logs) > 0, "elevated read also returns a non-empty log tail", failures)
        check(
            elevated_diagnostics.get("health") == owner_diagnostics.get("health"),
            "elevated read's health block matches exactly what the tenant owner saw", failures,
        )
        check(
            elevated_diagnostics.get("device_id") == owner_diagnostics.get("device_id"),
            "elevated read is for the same device", failures,
        )

        step(11, "THE key assertion: the elevated session, still under this active grant, cannot issue a device command (needs edge.manage, not requested)")
        status, refusal = api(
            f"/api/v1/tenant/edge/devices/{device_id}/commands",
            {"command_type": "diagnostic.ping", "idempotency_key": f"e2e-diag-{suffix}"},
            token=dev_a_token, extra_headers={"X-CSense-Support-Tenant-Id": tenant_id}, expect=(401, 403),
        )
        check(
            status in (401, 403),
            f"elevated write (issue command) refused (got {status}: {json.dumps(refusal)[:200]})", failures,
        )
        remaining = psql(
            f"SELECT count(*) FROM device_commands WHERE edge_device_id = '{device_id}' "
            f"AND idempotency_key = 'e2e-diag-{suffix}'"
        )
        check(remaining == "0", "no command row was created by the refused write", failures)

        step(12, "The elevated PATCH to the device (also edge.manage) is refused the same way")
        status, _ = api(
            f"/api/v1/tenant/edge/devices/{device_id}", {"name": "Renamed by elevated session"},
            token=dev_a_token, method="PATCH",
            extra_headers={"X-CSense-Support-Tenant-Id": tenant_id}, expect=(401, 403),
        )
        check(status in (401, 403), f"elevated PATCH device refused (got {status})", failures)

        step(13, "Even under elevation, the diagnostics response never carries a command's payload/signed_envelope")
        commands = elevated_diagnostics.get("commands") or []
        # Non-vacuous: step 5's owner-issued command is real command history for this
        # device, so this list is never empty - it genuinely exercises DiagnosticCommandOut's
        # narrowing rather than trivially passing over an empty list.
        check(len(commands) > 0, "the elevated read's command history is non-empty (step 5's command is in it)", failures)
        leaking = [c for c in commands if "payload" in c or "signed_envelope" in c]
        check(
            len(leaking) == 0,
            f"no command entry in the diagnostics response carries payload/signed_envelope ({len(commands)} command(s) checked)",
            failures,
        )

        step(14, "Developer B revokes the grant")
        status, revoked = api(
            f"/api/v1/admin/support-grants/{grant_id}/revoke",
            {"reason": "Investigation complete."}, dev_b_token, host="console.localhost", expect=(200,),
        )
        check(status == 200 and revoked["status"] == "revoked", "the grant is revoked", failures)

        step(15, "Elevation stops immediately - the identical elevated diagnostics call now fails (401)")
        status, _ = elevated_diagnostics_call(dev_a_token, device_id, tenant_id, expect=(401,))
        check(status == 401, "revoked grant no longer elevates -> 401", failures)

    finally:
        step("cleanup", "Stop and remove the container, the volume, and every test row")
        if container_started:
            docker("rm", "-f", container_name, check=False)
            print(f"    removed container {container_name}")
        if volume_created:
            docker("volume", "rm", volume_name, check=False)
            print(f"    removed volume {volume_name}")
        if tenant_id:
            # Cascades cameras, sites, edge_devices, edge_enrolment_tokens,
            # edge_health_events, device_commands and support_grants (all FK ...
            # ON DELETE CASCADE from tenants) - same discipline e2e_edge_spool.py and
            # e2e_support_grant_authorization.py already document for their own cases.
            psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
            psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
            psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
            leftover = psql(
                f"SELECT count(*) FROM edge_devices WHERE tenant_id = '{tenant_id}'"
            )
            check(leftover == "0", "no edge_devices rows survive the tenant's deletion", failures)
        if dev_a_email or dev_b_email:
            emails = ", ".join(f"'{e}'" for e in (dev_a_email, dev_b_email) if e)
            psql(f"DELETE FROM users WHERE email_normalized IN ({emails})")
        print("    test tenant, organization, users and grant rows removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "PASS - a real enrolled edge-agent device heartbeats a real content-bearing log "
        "tail; the tenant owner and a support-grant-elevated platform developer both read "
        "identical diagnostics through the same route; the elevated session is refused an "
        "unrelated write (issue command, PATCH device) proving read-only is enforced, not "
        "just intended; the diagnostics response never leaks command payload/signed_envelope "
        "under elevation; and revoking the grant stops elevation immediately"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
