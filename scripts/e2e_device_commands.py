"""Proves signed device commands work end-to-end for real (CHECKLIST: "Signed commands
with expiry/idempotency (desired-state push to the device)"): an operator issues a
command, a real enrolled device polls and receives it (marking it delivered), the signed
envelope actually verifies against the deployment's own running public key, a repeated
issuance with the same idempotency key returns the original command rather than a
duplicate, the device acks it, and a second device cannot ack the first device's command.

Real RS256 expiry/tamper-detection is unit-tested directly (test_signed_commands.py) -
this script proves the mechanism as wired into the real running API and a real enrolled
device credential, not the signing math in isolation.

    python scripts/e2e_device_commands.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

# Verifying the signed envelope locally needs the real JWT public key - the container's
# own JWT_PUBLIC_KEY_PATH (/run/secrets/jwt_public.pem) isn't reachable from this host
# process, but the same key file infra mounts it from is right here.
os.environ.setdefault(
    "JWT_PUBLIC_KEY_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra", "secrets", "jwt_public.pem"),
)

from csense_shared.config import get_settings
from csense_shared.security.signed_commands import verify_signed_command

API = "http://localhost:8080"
PASSWORD = "DeviceCommandsE2E!Password123"


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


def enrol_device(token: str, suffix: str, label: str) -> tuple[str, str]:
    """Mirrors e2e_edge_enrolment.py's own flow. Returns (device_id, agent_token)."""
    _, device = api("/api/v1/tenant/edge/devices", {
        "name": f"{label} {suffix}", "device_type": "jetson_orin", "role": "inference",
    }, token, expect=(201,))
    device_id = device["id"]

    _, issued = api(f"/api/v1/tenant/edge/devices/{device_id}/enrolment-token", {}, token, expect=(201,))
    enrolment_token = issued["token"]

    _, enrolled = api("/api/v1/tenant/edge/enrol", {
        "token": enrolment_token, "serial_number": f"JETSON-{label.upper()}-{suffix}",
        "device_type": "jetson_orin",
    }, expect=(201,))
    return device_id, enrolled["agent_token"]


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant and enrol two real devices")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Device Commands E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Ops",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    device_id, agent_token = enrol_device(token, suffix, "Gateway")
    _other_device_id, other_agent_token = enrol_device(token, suffix, "Other")

    step(2, "The operator issues a signed command")
    idempotency_key = f"apply-config-{suffix}"
    status, issued = api(f"/api/v1/tenant/edge/devices/{device_id}/commands", {
        "command_type": "config.apply", "payload": {"pipeline_version_id": str(uuid.uuid4())},
        "idempotency_key": idempotency_key,
    }, token, expect=(201,))
    check(status == 201, "issuance succeeds", failures)
    check(issued["status"] == "pending", "starts pending", failures)
    command_id = issued["id"]

    step(3, "The signed envelope actually verifies against the running deployment's own public key")
    settings = get_settings()
    claims = verify_signed_command(issued["signed_envelope"], settings=settings)
    check(str(claims.command_id) == command_id, "the verified command_id matches", failures)
    check(str(claims.edge_device_id) == device_id, "the verified edge_device_id matches", failures)
    check(claims.idempotency_key == idempotency_key, "the verified idempotency_key matches", failures)

    step(4, "Re-issuing with the same idempotency key returns the SAME command, not a duplicate")
    status, reissued = api(f"/api/v1/tenant/edge/devices/{device_id}/commands", {
        "command_type": "config.apply", "payload": {"different": "payload - ignored"},
        "idempotency_key": idempotency_key,
    }, token, expect=(201,))
    check(reissued["id"] == command_id, "idempotent reissuance returns the original command id", failures)
    check(reissued["signed_envelope"] == issued["signed_envelope"], "the identical signed envelope, not a new one", failures)

    step(5, "The real device polls and receives it, which marks it delivered")
    status, pending = api("/api/v1/tenant/edge/commands/pending", token=agent_token, method="GET")
    check(status == 200 and len(pending) == 1, "exactly one pending command for this device", failures)
    check(pending[0]["id"] == command_id, "it's the one that was issued", failures)

    _, pending_again = api("/api/v1/tenant/edge/commands/pending", token=agent_token, method="GET")
    check(pending_again == [], "delivered commands don't show up as pending again", failures)

    _, commands_after_poll = api(f"/api/v1/tenant/edge/devices/{device_id}/commands", token=token, method="GET")
    check(
        commands_after_poll[0]["status"] == "delivered" and commands_after_poll[0]["delivered_at"] is not None,
        "the operator's own view shows it delivered", failures,
    )

    step(6, "A different device cannot ack this device's command")
    status, _ = api(
        f"/api/v1/tenant/edge/commands/{command_id}/ack",
        {"result_code": "ok", "success": True}, other_agent_token, expect=(404,),
    )
    check(status == 404, "a different device's ack attempt is refused (404)", failures)

    step(7, "The real device acks it")
    status, acked = api(
        f"/api/v1/tenant/edge/commands/{command_id}/ack",
        {"result_code": "applied", "result_summary": "Config applied cleanly.", "success": True},
        agent_token, expect=(200,),
    )
    check(status == 200 and acked["status"] == "completed", "acking marks it completed", failures)
    check(acked["result_code"] == "applied", "the result code is recorded", failures)

    step(8, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Device Commands E2E {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'ops-{suffix}@northwind.example'")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - signed command issuance, verification, idempotency, delivery, and ack all work for real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
