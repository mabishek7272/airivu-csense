"""The onboarding path a new customer actually walks: site, then camera, in the browser.

This is the flow that was impossible until sites had a UI - the camera form asked for a
site id and there was no way to obtain one. It checks the whole path works, and three
specific behaviours around it:

  the camera form offers a *picker*, not a field to paste a UUID into
  a site that still has cameras cannot be deleted, and the refusal says why
  the timezone is presented as what it is - the zone rules are evaluated in

    python scripts/e2e_site_to_camera.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "SiteE2E!Password123"
OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states"
)


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
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"Site E2E {suffix}")
        page.get_by_label("Your name").fill("Onboarding Tester")
        page.get_by_label("Email").fill(f"sites-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)
        print("    signed in")

        print("\n[2] Cameras with no site tells you what to do first")
        page.goto(f"{BASE}/cameras")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first camera").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        # The site list is still in flight when the dialog opens, so wait for it to
        # settle. Reading immediately tested the loading state, not the empty one.
        dialog.locator(".notice-warning, select[name='site_id']").first.wait_for()
        body = dialog.inner_text()
        check(
            "no sites yet" in body.lower(),
            "the camera form says a site is needed before anything else",
            failures,
        )
        shot(page, "12-camera-needs-site")
        dialog.get_by_role("button", name="Cancel").click()

        print("\n[3] Create a site")
        page.goto(f"{BASE}/sites")
        page.wait_for_selector(".state-panel")
        check(
            "No sites yet" in page.locator(".state-panel").inner_text(),
            "sites starts empty with its own explanation",
            failures,
        )
        shot(page, "13-sites-empty")

        page.get_by_role("button", name="Add your first site").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        hint = dialog.inner_text()
        check(
            "rules are evaluated in this zone" in hint,
            "the timezone field says what it is actually used for",
            failures,
        )
        dialog.locator('[name="name"]').fill("Chennai Depot")
        dialog.locator('[name="code"]').fill(f"chennai-{suffix}")
        dialog.locator('[name="timezone"]').select_option("Asia/Kolkata")
        dialog.locator('[name="line1"]').fill("12 Anna Salai")
        dialog.locator('[name="city"]').fill("Chennai")
        dialog.get_by_role("button", name="Add site").click()

        page.wait_for_selector(".toast-success", timeout=15000)
        check(True, "the site was created", failures)
        page.wait_for_selector(".data-table")
        row = page.locator(".data-table tbody tr").first.inner_text()
        check("Asia/Kolkata" in row, "the listing shows the zone", failures)
        check(
            "now " in row,
            "the listing shows the current local time, so a wrong zone is obvious",
            failures,
        )
        shot(page, "14-site-created")

        print("\n[4] The camera form now offers a picker")
        page.goto(f"{BASE}/cameras")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first camera").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        site_field = dialog.locator('[name="site_id"]')
        check(
            site_field.evaluate("el => el.tagName.toLowerCase()") == "select",
            "the site field is a picker, not a box to paste a UUID into",
            failures,
        )
        options = site_field.locator("option").all_inner_texts()
        check(
            any("Chennai Depot" in o and "Asia/Kolkata" in o for o in options),
            "the picker names the site and its timezone",
            failures,
        )
        shot(page, "15-camera-site-picker")

        print("\n[5] Create a camera in that site")
        dialog.locator('[name="name"]').fill("Loading Bay 2")
        dialog.locator('[name="code"]').fill(f"bay-{suffix}")
        site_field.select_option(label=next(o for o in options if "Chennai" in o))
        dialog.locator('[name="hostname"]').fill("nvr.example.com")
        dialog.locator('[name="main_stream_path"]').fill("/Streaming/Channels/101")
        dialog.get_by_role("button", name="Add camera").click()

        page.wait_for_selector(".toast-success, .error-summary", timeout=15000)
        created = page.locator(".toast-success").count() > 0
        check(created, "the camera was created against the chosen site", failures)
        if created:
            page.wait_for_selector(".data-table")
            check(
                "Chennai Depot" in page.locator(".data-table").inner_text(),
                "the camera listing shows which site it belongs to",
                failures,
            )
            shot(page, "16-camera-created")

        print("\n[6] A site with cameras cannot be deleted")
        page.goto(f"{BASE}/sites")
        page.wait_for_selector(".data-table")
        listing = page.locator(".data-table").inner_text()
        check("1 camera" in listing, "the site listing counts what is attached", failures)

        page.get_by_role("button", name="Remove").first.click()
        page.wait_for_selector("div[role='dialog']")
        warning = page.locator("div[role='dialog']").inner_text()
        check(
            "still has things attached" in warning,
            "the confirmation warns before the click, not after the refusal",
            failures,
        )
        shot(page, "17-site-delete-blocked")

        page.get_by_role("button", name="Remove site").click()
        page.wait_for_selector(".toast-error", timeout=15000)
        message = page.locator(".toast-error").inner_text()
        check(
            "1 camera" in message,
            "the server refuses and names what is still attached",
            failures,
        )
        shot(page, "18-site-delete-refused")

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - a customer can go from nothing to a working camera")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
