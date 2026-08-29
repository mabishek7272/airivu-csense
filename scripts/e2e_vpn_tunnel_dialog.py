"""Drives the Edge page's "Tunnel" dialog in a browser.

The API side is proven end-to-end by e2e_vpn_provisioning.py, including the cross-tenant
collision checks - a browser test cannot exercise two tenants at once through one page any
more easily than that script already does. What this checks is the one thing that script
cannot: that an operator clicking "Tunnel" actually sees a real, allocated address and both
config blocks, not a broken dialog wired to the endpoint.

    python scripts/e2e_vpn_tunnel_dialog.py [--headed]
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "VpnDialogE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register and add a device")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"VPN Dialog E2E {suffix}")
        page.get_by_label("Your name").fill("Dialog Tester")
        page.get_by_label("Email").fill(f"vpnui-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)

        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first device").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("Loading dock gateway")
        d.get_by_role("button", name="Add device").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        print("\n[2] A device with no WireGuard key cannot be tunnelled yet")
        page.wait_for_selector(".data-table")
        row = page.locator(".data-table tbody tr").first
        tunnel_button = row.get_by_role("button", name="Tunnel")
        check(not tunnel_button.is_enabled(), "the Tunnel action is disabled pre-enrolment",
              failures)
        shot(page, "29-tunnel-disabled-pre-enrolment")

        print("\n[3] Enrol the device with a WireGuard key, via the API")
        # Redeeming an enrolment token is a device action with its own dedicated flow
        # (proven by e2e_edge_enrolment.py and e2e_vpn_provisioning.py) - reproducing it
        # by clicking through would test typing into a form, not the tunnel dialog this
        # script exists for.
        row.get_by_role("button", name="Enrol").click()
        page.wait_for_selector("div[role='dialog']")
        token_text = page.locator(".token-display code").inner_text()
        page.get_by_role("button", name="Done").click()

        # Same host Playwright's browser context resolves `app.localhost` to - but Python's
        # own resolver has no special case for `*.localhost` the way a browser does, so
        # this connects to the plain host:port and names the tenant via the Host header,
        # exactly like every other script here that calls the API directly.
        request = urllib.request.Request(
            "http://localhost:8080/api/v1/tenant/edge/enrol",
            data=json.dumps({
                "token": token_text,
                "serial_number": f"SN-{suffix}",
                "wireguard_public_key": base64.b64encode(os.urandom(32)).decode(),
            }).encode(),
            headers={"Content-Type": "application/json", "Host": "app.localhost"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=30)
        print("    enrolled with a WireGuard key")

        print("\n[4] Reload and open the Tunnel dialog")
        page.reload()
        page.wait_for_selector(".data-table")
        row = page.locator(".data-table tbody tr").first
        row.get_by_role("button", name="Tunnel").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        # Only rendered once provisioning has actually completed, so waiting on it is
        # waiting on the real thing rather than guessing a timeout.
        dialog.locator("#tunnel-lan-cidr").wait_for(timeout=15000)

        body = dialog.inner_text()
        check("10.8.0." in body, "a real pool address was allocated, not a placeholder",
              failures)
        check("10.0.0.2" not in body, "the guide's hardcoded address never appears", failures)
        check("[Peer]" in body, "the server peer stanza is rendered", failures)
        check("[Interface]" in body, "the client config is rendered", failures)
        check("/24" not in body, "no /24 AllowedIPs appears anywhere in the dialog", failures)
        shot(page, "30-tunnel-provisioned")

        print("\n[5] Declaring the host's own Docker bridge as a site LAN is refused")
        dialog.locator("#tunnel-lan-cidr").fill("172.18.0.0/16")
        dialog.get_by_role("button", name="Save").click()
        page.wait_for_selector(".notice-warning", timeout=15000)
        warning = dialog.locator(".notice-warning").inner_text()
        check("Could not update the tunnel" in warning,
              "the rejection is shown inline, not just logged", failures)
        shot(page, "31-tunnel-lan-rejected")

        print("\n[6] A real site LAN is accepted and reflected in the dialog")
        dialog.locator("#tunnel-lan-cidr").fill("192.168.77.0/24")
        dialog.get_by_role("button", name="Save").click()
        # Not `.toast-success` here: a stale toast from step [1]'s device creation could
        # still be on screen and satisfy that wait before this save has even returned. The
        # LAN text actually changing is the real assertion.
        dialog.get_by_text("192.168.77.0/24").wait_for(timeout=15000)
        check(True, "the saved LAN appears in the dialog once the update completes", failures)

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the Tunnel dialog allocates and renders a real, collision-free config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
