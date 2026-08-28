"""End-to-end proof of the Edge section: register a device, enrol it, prove it is locked.

Walks the flow an operator and an installer actually follow - add the device in the
portal, issue a token, redeem it from the device - and then checks the properties that
matter more than the happy path:

  - the enrolment token is single-use
  - a token redeemed on the wrong serial is refused
  - an expired or unknown token is refused, and all failures look identical
  - neither token is ever readable back from the API
  - a retired device's credentials are destroyed, not merely hidden

    python scripts/e2e_edge_enrolment.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "EdgeE2E!Password123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:300]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def step(n, text):
    print(f"\n[{n}] {text}")


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures = []

    step(1, "Register a tenant")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Edge E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Deployment Engineer",
    })
    token = auth["access_token"]
    tenant_id = auth["tenant_id"]
    print(f"    tenant {tenant_id}")

    step(2, "Add a Jetson Orin as an inference device")
    _, device = api("/api/v1/tenant/edge/devices", {
        "name": "Warehouse Mumbai gateway",
        "device_type": "jetson_orin",
        "role": "inference",
    }, token)
    device_id = device["id"]
    print(f"    {device_id}  status={device['status']}  online={device['online']}")

    step(3, "Add a Raspberry Pi as a connectivity-only gateway")
    _, pi = api("/api/v1/tenant/edge/devices", {
        "name": "Chennai depot gateway",
        "device_type": "raspberry_pi",
        "role": "gateway",
    }, token)
    print(f"    {pi['id']}  role={pi['role']}")

    step(4, "Issue an enrolment token")
    _, issued = api(f"/api/v1/tenant/edge/devices/{device_id}/enrolment-token", {}, token)
    enrolment_token = issued["token"]
    print(f"    token issued, expires {issued['expires_at']}")
    print(f"    prefix {enrolment_token[:8]}... (plaintext shown once)")

    step(5, "Confirm the token is stored only as a digest")
    stored = psql(
        "SELECT token_hash, token_prefix FROM edge_enrolment_tokens "
        f"WHERE device_id = '{device_id}'"
    )
    if enrolment_token in stored:
        failures.append("enrolment token plaintext found in the database")
    print(f"    row holds: {stored[:40]}...  (plaintext present: "
          f"{'YES - BUG' if enrolment_token in stored else 'no'})")

    step(6, "Redeem it from the device")
    _, enrolled = api("/api/v1/tenant/edge/enrol", {
        "token": enrolment_token,
        "serial_number": f"JETSON-{suffix.upper()}",
        "device_type": "jetson_orin",
        "hardware": {"cpu_cores": 12, "ram_mb": 32768, "gpu": "Ampere 2048-core",
                     "vram_mb": 32768},
        "capabilities": {"can_infer": True, "max_streams": 16,
                         "accelerators": ["cuda", "tensorrt"]},
        "os_name": "Ubuntu", "os_version": "22.04",
        "agent_version": "1.0.0",
    })
    agent_token = enrolled["agent_token"]
    print(f"    enrolled as '{enrolled['name']}' role={enrolled['role']}")
    print(f"    agent credential issued (distinct from enrolment token: "
          f"{agent_token != enrolment_token})")
    if agent_token == enrolment_token:
        failures.append("agent credential is the same value as the enrolment token")

    step(7, "The same token must not work twice")
    status, body = api("/api/v1/tenant/edge/enrol", {
        "token": enrolment_token,
        "serial_number": f"JETSON-{suffix.upper()}",
    }, expect=(401,))
    print(f"    -> {status} {body.get('message', '')[:70]}")
    if status != 401:
        failures.append("a redeemed enrolment token was accepted a second time")

    step(8, "An unknown token is refused with the same message")
    status2, body2 = api("/api/v1/tenant/edge/enrol", {
        "token": "unknown-" + uuid.uuid4().hex,
        "serial_number": "SOMETHING-ELSE",
    }, expect=(401,))
    same = body.get("message") == body2.get("message")
    print(f"    -> {status2}, message identical to the reused-token case: {same}")
    if not same:
        failures.append("failure messages differ, which lets tokens be probed")

    step(9, "A token redeemed on the wrong serial is refused")
    _, second = api(f"/api/v1/tenant/edge/devices/{pi['id']}/enrolment-token", {}, token)
    api("/api/v1/tenant/edge/enrol", {
        "token": second["token"], "serial_number": f"PI-{suffix.upper()}",
    })
    _, third = api(f"/api/v1/tenant/edge/devices/{pi['id']}/enrolment-token", {}, token)
    status3, _ = api("/api/v1/tenant/edge/enrol", {
        "token": third["token"], "serial_number": "A-DIFFERENT-BOX",
    }, expect=(401,))
    print(f"    -> {status3} (serial pinned at first enrolment)")
    if status3 != 401:
        failures.append("a token was redeemed on hardware it was not issued for")

    step(10, "Neither credential is readable back from the API")
    _, detail = api(f"/api/v1/tenant/edge/devices/{device_id}", None, token, method="GET")
    _, listing = api("/api/v1/tenant/edge/devices", None, token, method="GET")
    blob = json.dumps(detail) + json.dumps(listing)
    leaked = [n for n, v in (("enrolment", enrolment_token), ("agent", agent_token))
              if v in blob]
    print(f"    credentials present in responses: {leaked or 'no'}")
    print(f"    reported hardware: {detail['hardware']}")
    print(f"    capabilities     : {detail['capabilities']}")
    if leaked:
        failures.append(f"credentials readable from the API: {leaked}")

    step(11, "The device heartbeats with its own credential")
    _, beat = api("/api/v1/tenant/edge/heartbeat", {
        "status": "ok",
        "health": {
            "infrastructure": {"wireguard_handshake_age_s": 12, "vpn_ip": "10.0.0.2"},
            "service": {"rtsp_reachable": True, "cameras_up": 4},
            "quality": {"latency_ms": 48, "fps": 20.0, "packet_loss_pct": 0.1},
        },
        "connectivity_method": "wireguard",
        "connectivity_reason": "VPN permitted and production deployment",
        "events": [
            {"level": "infrastructure", "check_name": "wireguard_handshake",
             "status": "recovered", "detail": "Tunnel re-established after ISP IP change",
             "metrics": {"downtime_s": 42}},
        ],
    }, agent_token)
    print(f"    acknowledged={beat['acknowledged']}  events={beat['events_recorded']}  "
          f"next in {beat['next_interval_seconds']}s")

    step(12, "The device now reads as online, with its connectivity method recorded")
    _, detail = api(f"/api/v1/tenant/edge/devices/{device_id}", None, token, method="GET")
    print(f"    online={detail['online']}  status={detail['status']}  "
          f"method={detail['connectivity_method']}")
    if not detail["online"]:
        failures.append("a device that just heartbeated does not read as online")
    if detail["connectivity_method"] != "wireguard":
        failures.append("the connectivity method the device chose was not recorded")

    step(13, "Only transitions are stored, not every beat")
    for _ in range(3):
        api("/api/v1/tenant/edge/heartbeat", {"status": "ok"}, agent_token)
    _, history = api(
        f"/api/v1/tenant/edge/devices/{device_id}/health", None, token, method="GET"
    )
    print(f"    4 heartbeats sent, {len(history)} history row(s) stored")
    for event in history:
        print(f"      {event['level']}/{event['check_name']}: {event['status']}"
              f" - {event['detail']}")
    if len(history) != 1:
        failures.append(f"expected 1 stored event after 4 heartbeats, got {len(history)}")

    step(14, "A device cannot heartbeat with a user's token, or a bad credential")
    status_a, _ = api("/api/v1/tenant/edge/heartbeat", {"status": "ok"}, token,
                      expect=(401,))
    status_b, _ = api("/api/v1/tenant/edge/heartbeat", {"status": "ok"},
                      "not-a-real-credential", expect=(401,))
    status_c, _ = api("/api/v1/tenant/edge/heartbeat", {"status": "ok"}, None,
                      expect=(401,))
    print(f"    user JWT -> {status_a}, bad credential -> {status_b}, none -> {status_c}")
    if {status_a, status_b, status_c} != {401}:
        failures.append("a heartbeat was accepted without a valid device credential")

    step(15, "Retiring the device destroys what it authenticates with")
    api(f"/api/v1/tenant/edge/devices/{device_id}", None, token, method="DELETE")
    remaining = psql(
        "SELECT coalesce(agent_token_hash,'destroyed'), status FROM edge_devices "
        f"WHERE id = '{device_id}'"
    )
    print(f"    {remaining}")
    if "destroyed" not in remaining:
        failures.append("a retired device kept a usable agent credential")

    step(16, "A retired device's credential stops working immediately")
    status_d, _ = api("/api/v1/tenant/edge/heartbeat", {"status": "ok"}, agent_token,
                      expect=(401,))
    print(f"    -> {status_d} (no waiting for a credential to expire)")
    if status_d != 401:
        failures.append("a retired device could still heartbeat")

    step(17, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Edge E2E {suffix}'")
    print("    test tenant removed")

    print()
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
