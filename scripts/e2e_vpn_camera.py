"""Proves a camera behind a WireGuard peer is reachable, and nothing else is.

The recommended production deployment puts the camera at 10.0.0.2:554. The SSRF guard
originally refused every private address, which blocked that path outright. This checks
the corrected behaviour from both directions:

  a camera whose edge device has been allocated 10.0.0.2 may be probed at 10.0.0.2
  the same tenant may not probe 10.0.0.3, which is someone else's peer
  a camera on the site LAN the peer routes may be probed
  loopback and the metadata endpoint stay refused even with a tunnel provisioned
  a camera in `direct` mode gets no exception at all

There is no WireGuard tunnel in the local stack, so "reachable" here means the guard
permitted the attempt and the probe failed on connection rather than on policy. That is
the distinction being tested: `address_not_permitted` versus a plain connection failure.

    python scripts/e2e_vpn_camera.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "VpnE2E!Password123"


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
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def step(n, text):
    print(f"\n[{n}] {text}")


def probe(camera_id, token):
    """Returns (blocked_by_policy, detail)."""
    status, body = api(
        f"/api/v1/tenant/cameras/{camera_id}/probe", {}, token, expect=(200, 422)
    )
    if status == 422 and body.get("code") == "address_not_permitted":
        return True, body.get("message", "")
    return False, body.get("detail") or body.get("message", "")


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures = []

    step(1, "Register a tenant and a site")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"VPN E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Deployment Engineer",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]
    site_id = psql(
        "INSERT INTO sites (tenant_id, name, code, timezone) VALUES "
        f"('{tenant_id}', 'Warehouse Mumbai', 'site-{suffix}', 'Asia/Kolkata') RETURNING id"
    )
    print(f"    tenant {tenant_id}")

    step(2, "Add an edge device and provision it a VPN address and site LAN")
    _, device = api("/api/v1/tenant/edge/devices", {
        "name": "Mumbai gateway", "site_id": site_id,
        "device_type": "raspberry_pi", "role": "gateway",
    }, token)
    # Allocation is a platform concern and has no API yet; this stands in for it.
    psql(
        f"UPDATE edge_devices SET vpn_address = '10.0.0.2', lan_cidr = '192.168.1.0/24' "
        f"WHERE id = '{device['id']}'"
    )
    print(f"    {device['id']} -> 10.0.0.2/32, routes 192.168.1.0/24")

    step(3, "A camera at the peer's own address may be probed")
    _, cam_peer = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "NVR via tunnel", "code": f"peer-{suffix}",
        "hostname": "10.0.0.2", "rtsp_port": 554,
        "main_stream_path": "/Streaming/Channels/101",
    }, token)
    psql(
        f"UPDATE cameras SET connection_mode = 'vpn', edge_device_id = '{device['id']}' "
        f"WHERE id = '{cam_peer['id']}'"
    )
    blocked, detail = probe(cam_peer["id"], token)
    print(f"    blocked by policy: {blocked}")
    print(f"    {detail[:100]}")
    if blocked:
        failures.append("a provisioned peer address was refused - the guard still blocks VPN")

    step(4, "A camera on the routed site LAN may be probed")
    _, cam_lan = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Dock camera", "code": f"lan-{suffix}",
        "hostname": "192.168.1.100", "rtsp_port": 554, "main_stream_path": "/live",
    }, token)
    psql(
        f"UPDATE cameras SET connection_mode = 'vpn', edge_device_id = '{device['id']}' "
        f"WHERE id = '{cam_lan['id']}'"
    )
    blocked, detail = probe(cam_lan["id"], token)
    print(f"    blocked by policy: {blocked}")
    if blocked:
        failures.append("an address on the provisioned site LAN was refused")

    step(5, "Another peer's address is NOT reachable, even with a tunnel provisioned")
    _, cam_other = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Someone else's peer", "code": f"other-{suffix}",
        "hostname": "10.0.0.3", "rtsp_port": 554, "main_stream_path": "/live",
    }, token)
    psql(
        f"UPDATE cameras SET connection_mode = 'vpn', edge_device_id = '{device['id']}' "
        f"WHERE id = '{cam_other['id']}'"
    )
    blocked, detail = probe(cam_other["id"], token)
    print(f"    blocked by policy: {blocked}")
    print(f"    {detail[:110]}")
    if not blocked:
        failures.append("10.0.0.3 was reachable - the allowlist is a subnet, not a host")

    step(6, "Loopback and the metadata endpoint stay refused")
    for label, host in (("loopback", "127.0.0.1"), ("metadata", "169.254.169.254")):
        _, cam = api("/api/v1/tenant/cameras", {
            "site_id": site_id, "name": f"internal {label}",
            "code": f"{label}-{suffix}", "hostname": host, "rtsp_port": 554,
            "main_stream_path": "/live",
        }, token)
        psql(
            f"UPDATE cameras SET connection_mode = 'vpn', "
            f"edge_device_id = '{device['id']}' WHERE id = '{cam['id']}'"
        )
        blocked, _ = probe(cam["id"], token)
        print(f"    {label} ({host}) blocked: {blocked}")
        if not blocked:
            failures.append(f"{host} was reachable through a tunnel allowlist")

    step(7, "A direct-mode camera gets no private-address exception")
    _, cam_direct = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Direct but private", "code": f"direct-{suffix}",
        "hostname": "10.0.0.2", "rtsp_port": 554, "main_stream_path": "/live",
    }, token)
    blocked, _ = probe(cam_direct["id"], token)
    print(f"    same address, connection_mode=direct, blocked: {blocked}")
    if not blocked:
        failures.append("a direct-mode camera reached a private address without a tunnel")

    step(8, "The database refuses a tunnel camera with no edge device")
    rejected = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc",
         (f"UPDATE cameras SET connection_mode = 'vpn', edge_device_id = NULL "
          f"WHERE id = '{cam_direct['id']}'")],
        cwd="infra", capture_output=True, text=True, check=False,
    )
    enforced = rejected.returncode != 0
    print(f"    constraint enforced: {enforced}")
    if not enforced:
        failures.append("a camera could claim VPN mode with no device to scope it")

    step(9, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'VPN E2E {suffix}'")
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
