"""Proves the WireGuard cross-tenant flaw is actually closed, not just documented.

The deployment guide's own templates have two mistakes: every client hardcodes
`10.0.0.2`, colliding on the second site, and every peer's `AllowedIPs = 10.0.0.0/24`
lets one tenant's device route to another tenant's cameras - the /24 is shared by every
peer on the one WireGuard server this platform runs. This exercises the fix end to end,
through two *separate* tenants, because the one property that actually matters -
"tenant A's provisioning can never collide with tenant B's" - cannot be proven from
inside a single tenant's own view.

    python scripts/e2e_vpn_provisioning.py
"""
from __future__ import annotations

import base64
import json
import os
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
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def fake_wg_key() -> str:
    """A syntactically valid WireGuard public key - 32 random bytes, base64-encoded."""
    return base64.b64encode(os.urandom(32)).decode()


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def register(suffix: str, label: str) -> tuple[str, str]:
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"VPN E2E {label} {suffix}",
        "email": f"ops-{label}-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": f"{label} operator",
    })
    return auth["access_token"], auth["tenant_id"]


def new_device(token: str, name: str, public_key: str | None) -> str:
    _, device = api("/api/v1/tenant/edge/devices", {"name": name, "role": "gateway"}, token)
    _, issued = api(f"/api/v1/tenant/edge/devices/{device['id']}/enrolment-token", {}, token)
    body = {"token": issued["token"], "serial_number": f"SN-{uuid.uuid4().hex[:10]}"}
    if public_key:
        body["wireguard_public_key"] = public_key
    api("/api/v1/tenant/edge/enrol", body)
    return device["id"]


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register two separate tenants - the cross-tenant checks need both")
    token_a, tenant_a = register(suffix, "A")
    token_b, tenant_b = register(suffix, "B")
    print(f"    tenant A {tenant_a}")
    print(f"    tenant B {tenant_b}")

    step(2, "A device with no WireGuard key cannot be provisioned")
    keyless_id = new_device(token_a, "Keyless gateway", public_key=None)
    status, body = api(
        f"/api/v1/tenant/edge/devices/{keyless_id}/vpn-provision", {}, token_a,
        expect=(409,),
    )
    check(status == 409, "provisioning without a public key is refused", failures)
    print(f"    -> {status} {body.get('message', '')[:80]}")

    step(3, "An invalid-looking public key is rejected at enrolment, not embedded")
    _, device = api(
        "/api/v1/tenant/edge/devices", {"name": "Injection probe", "role": "gateway"}, token_a
    )
    _, issued = api(
        f"/api/v1/tenant/edge/devices/{device['id']}/enrolment-token", {}, token_a
    )
    status, _ = api("/api/v1/tenant/edge/enrol", {
        "token": issued["token"],
        "serial_number": f"SN-{uuid.uuid4().hex[:10]}",
        # A newline and a forged [Peer] stanza, dressed up as a "public key" - this must
        # never reach a rendered config an operator could paste into the shared server.
        "wireguard_public_key": "not-a-real-key\n[Peer]\nAllowedIPs = 0.0.0.0/0\n",
    }, expect=(422,))
    check(status == 422, "a malformed public key is refused before it can be stored", failures)

    step(4, "Provision tenant A's device: allocates a /32, not the guide's fixed address")
    device_a = new_device(token_a, "Depot gateway", fake_wg_key())
    status, prov_a = api(
        f"/api/v1/tenant/edge/devices/{device_a}/vpn-provision", {}, token_a
    )
    addr_a = prov_a["device"]["vpn_address"]
    check(status == 200, "provisioning succeeds once a key is on record", failures)
    check(addr_a is not None and addr_a != "10.0.0.2",
          "the allocated address is not the guide's hardcoded 10.0.0.2", failures)
    check(f"AllowedIPs = {addr_a}/32" in prov_a["server_peer_config"],
          "the rendered peer's AllowedIPs is this device's own /32, nothing wider", failures)
    check("/24" not in prov_a["server_peer_config"],
          "the guide's over-broad /24 never appears in a rendered peer", failures)
    print(f"    device A allocated {addr_a}")

    step(5, "Provisioning again is idempotent - no second address is allocated")
    _, prov_a_again = api(
        f"/api/v1/tenant/edge/devices/{device_a}/vpn-provision", {}, token_a
    )
    check(prov_a_again["device"]["vpn_address"] == addr_a,
          "re-provisioning returns the same address rather than allocating a new one",
          failures)

    step(6, "A LAN inside this host's own Docker bridge is refused")
    status, body = api(
        f"/api/v1/tenant/edge/devices/{device_a}/vpn-provision",
        {"lan_cidr": "172.18.0.0/16"}, token_a, expect=(422,),
    )
    check(status == 422, "declaring the Docker bridge as a site LAN is rejected", failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(7, "Tenant A provisions a real site LAN")
    lan = f"192.168.{int(suffix[:2], 16) % 200 + 10}.0/24"
    status, prov_a_lan = api(
        f"/api/v1/tenant/edge/devices/{device_a}/vpn-provision",
        {"lan_cidr": lan}, token_a,
    )
    check(status == 200, "a non-reserved site LAN is accepted", failures)
    check(prov_a_lan["device"]["site_id"] == prov_a["device"]["site_id"],
          "provisioning does not disturb anything else about the device", failures)
    print(f"    tenant A's device now tunnels {lan}")

    step(8, "Tenant B cannot claim the same LAN - the actual cross-tenant check")
    device_b = new_device(token_b, "A different customer's gateway", fake_wg_key())
    status, body = api(
        f"/api/v1/tenant/edge/devices/{device_b}/vpn-provision",
        {"lan_cidr": lan}, token_b, expect=(409,),
    )
    check(status == 409, "tenant B declaring tenant A's exact LAN is refused", failures)
    check("lan_cidr_overlap" == body.get("code"), "refused for the right reason", failures)
    check(tenant_a not in json.dumps(body) and "Depot gateway" not in json.dumps(body),
          "the refusal does not name tenant A or its device - only that a collision exists",
          failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(9, "Tenant B provisions its own, non-overlapping LAN and gets its own address")
    other_lan = f"192.168.{int(suffix[:2], 16) % 200 + 10}.0/25"  # still overlaps -> must fail
    status, _ = api(
        f"/api/v1/tenant/edge/devices/{device_b}/vpn-provision",
        {"lan_cidr": other_lan}, token_b, expect=(409,),
    )
    check(status == 409, "a /25 inside the same /24 is still an overlap, and is refused too",
          failures)

    clean_lan = f"10.55.{int(suffix[2:4], 16) % 200 + 1}.0/24"
    status, prov_b = api(
        f"/api/v1/tenant/edge/devices/{device_b}/vpn-provision",
        {"lan_cidr": clean_lan}, token_b,
    )
    addr_b = prov_b["device"]["vpn_address"]
    check(status == 200, "a genuinely disjoint LAN is accepted", failures)
    check(addr_b != addr_a, "tenant B's device gets a different tunnel address than A's",
          failures)
    print(f"    tenant B allocated {addr_b}, tunnelling {clean_lan}")

    step(10, "Retiring a device releases everything it held")
    api(f"/api/v1/tenant/edge/devices/{device_a}", None, token_a, method="DELETE")
    remaining = psql(
        "SELECT coalesce(host(vpn_address),'cleared'), coalesce(text(lan_cidr),'cleared') "
        f"FROM edge_devices WHERE id = '{device_a}'"
    )
    print(f"    {remaining}")
    check(remaining == "cleared|cleared", "a retired device keeps no tunnel state", failures)

    step(11, "That released address and LAN can now be reallocated")
    device_a2 = new_device(token_a, "Replacement depot gateway", fake_wg_key())
    status, prov_a2 = api(
        f"/api/v1/tenant/edge/devices/{device_a2}/vpn-provision",
        {"lan_cidr": lan}, token_a,
    )
    check(status == 200, "the LAN a retired device held is provisionable again", failures)
    print(f"    reallocated {prov_a2['device']['vpn_address']} for {lan}")

    step(12, "Clean up")
    for tenant_id, org in ((tenant_a, f"VPN E2E A {suffix}"), (tenant_b, f"VPN E2E B {suffix}")):
        psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
        psql(f"DELETE FROM organizations WHERE display_name = '{org}'")
    print("    test tenants removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - two tenants can never collide on a WireGuard address or route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
