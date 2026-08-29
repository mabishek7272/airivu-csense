"""Proves NVR channel discovery works end-to-end, and that the ONVIF stub is honest.

Registers a throwaway tenant, discovers channels from a (mock) NVR through the real API,
turns two of them into real cameras the normal way, and confirms the ONVIF-discovery
endpoint reports itself unavailable with a real reason rather than faking a scan. Also
confirms the address guard actually holds: a private-range host with no associated edge
device is refused, the same rule `camera_probe.py` already enforces for a `direct`-mode
camera.

    python scripts/e2e_nvr_discovery.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "NvrDiscoveryE2E!Password123"


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

    step(1, "Register a tenant and a site")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"NVR Discovery E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "NVR Discovery Tester",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Discovery Site", "code": f"discovery-{suffix}", "timezone": "Asia/Kolkata",
    }, token)

    step(2, "Discover channels from a (public-address) mock NVR")
    _, discovery = api("/api/v1/tenant/nvr/discover", {
        "hostname": "8.8.8.8", "port": 8899, "username": "admin", "password": "admin123",
    }, token)
    channels = discovery["channels"]
    check(len(channels) >= 1, "at least one channel came back", failures)
    check(
        all(c["main_stream_path"] and c["channel_id"] and c["name"] for c in channels),
        "every channel has an id, a name and a main stream path", failures,
    )
    check(
        all(c["vendor"] == "Mock NVR" for c in channels),
        "the mock adapter's channels are clearly labelled, not passed off as real", failures,
    )

    step(3, "A private-range host with no associated edge device is refused")
    status, body = api("/api/v1/tenant/nvr/discover", {
        "hostname": "10.1.2.3", "port": 80, "username": "admin", "password": "admin123",
    }, token, expect=(422,))
    check(status == 422, "private address without a device is rejected (422)", failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(4, "Turn two discovered channels into real cameras")
    created = []
    for channel in channels[:2]:
        _, camera = api("/api/v1/tenant/cameras", {
            "site_id": site["id"], "name": channel["name"], "code": f"ch-{channel['channel_id']}-{suffix}",
            "hostname": "8.8.8.8", "rtsp_port": 8899,
            "main_stream_path": channel["main_stream_path"],
            "sub_stream_path": channel["sub_stream_path"],
        }, token)
        created.append(camera)
    check(len(created) == 2, "both channels became real camera rows", failures)
    check(
        created[0]["main_stream_path"] == channels[0]["main_stream_path"],
        "the created camera's stream path matches the discovered channel exactly", failures,
    )

    step(5, "ONVIF discovery reports itself unavailable, honestly - not a fake scan")
    _, onvif = api(f"/api/v1/tenant/sites/{site['id']}/discover-cameras", {}, token)
    check(onvif["available"] is False, "available: false (no edge agent exists yet)", failures)
    check(len(onvif.get("reason", "")) > 20, "a real, specific reason is given", failures)
    check(onvif.get("cameras", []) == [], "no fake results are returned alongside it", failures)

    step(6, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'NVR Discovery E2E {suffix}'")
    print("    test tenant removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - NVR discovery works end-to-end; ONVIF discovery is honestly stubbed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
