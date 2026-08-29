"""Drives the Rules page in a browser: create, the night-confidence warning, enable/disable.

The API side is proven end-to-end by e2e_rule_to_incident.py. This checks the form built
on top of it - specifically that the warning a risky rule carries is visible at the moment
someone is setting the threshold, not just in the API response they never look at.

    python scripts/e2e_rule_editor.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "RuleUiE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def click_canvas(page, fx: float, fy: float) -> None:
    box = page.locator(".polygon-canvas").bounding_box()
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register, and build a site + camera + zone to hang a rule on")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"Rule UI E2E {suffix}")
        page.get_by_label("Your name").fill("Rule UI Tester")
        page.get_by_label("Email").fill(f"ruleui-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)

        page.goto(f"{BASE}/sites")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first site").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("Depot")
        d.locator('[name="code"]').fill(f"depot-{suffix}")
        d.get_by_role("button", name="Add site").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.goto(f"{BASE}/cameras")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first camera").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator(".notice-warning, select[name='site_id']").first.wait_for()
        d.locator('[name="name"]').fill("Dock camera")
        d.locator('[name="code"]').fill(f"dock-{suffix}")
        d.locator('select[name="site_id"]').select_option(index=1)
        d.get_by_role("button", name="Add camera").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.goto(f"{BASE}/zones")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Draw your first zone").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("Restricted dock")
        for fx, fy in ((0.30, 0.30), (0.75, 0.30), (0.75, 0.80), (0.30, 0.80)):
            click_canvas(page, fx, fy)
            page.wait_for_timeout(100)
        d.get_by_role("button", name="Create zone").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        print("    site, camera and zone ready")

        print("\n[2] Rules starts empty and explains the consequence of having none")
        page.goto(f"{BASE}/rules")
        page.wait_for_selector(".state-panel")
        body = page.locator(".state-panel").inner_text()
        check("No rules yet" in body, "the empty state is specific to rules", failures)
        check(
            "nothing alerts" in body,
            "it states what happens without one, not just that none exist",
            failures,
        )
        shot(page, "25-rules-empty")

        print("\n[3] A high confidence threshold is flagged while it is being set")
        page.get_by_role("button", name="Create your first rule").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("Too strict")
        d.locator('[name="min_confidence"]').fill("0.9")
        d.locator('[name="min_confidence"]').blur()
        page.wait_for_selector(".notice-warning")
        warning = page.locator(".notice-warning").inner_text()
        check(
            "night" in warning.lower(),
            "the risk is explained, not just flagged",
            failures,
        )
        shot(page, "26-rule-night-warning")

        print("\n[4] Create a workable rule, scoped to the zone")
        d.locator('[name="min_confidence"]').fill("0.4")
        d.locator('select[name="zone_id"]').select_option(index=1)
        d.get_by_role("button", name="Create rule").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        page.wait_for_selector(".data-table")
        row = page.locator(".data-table tbody tr").first.inner_text()
        check("Restricted dock" in row, "the listing shows which zone it watches", failures)
        check("40% confidence" in row, "the listing shows the threshold", failures)
        shot(page, "27-rule-created")

        print("\n[5] Disable it, then re-enable it")
        # Not `.toast-success` here: the create toast from step 4 has not yet auto-
        # dismissed (its 5s timer is still running), so that selector would match the
        # *stale* toast the instant Disable is clicked - before the request completes -
        # and the check would race ahead of the actual state change. Waiting on the pill
        # text itself is what the assertion is really about.
        status_pill = page.locator(".data-table tbody tr").first.locator(".pill").first
        page.get_by_role("button", name="Disable").click()
        status_pill.get_by_text("disabled").wait_for(timeout=15000)
        check(True, "disabling is reflected once the request completes", failures)
        shot(page, "28-rule-disabled")

        page.get_by_role("button", name="Enable").click()
        status_pill.get_by_text("active").wait_for(timeout=15000)
        check(True, "re-enabling is reflected once the request completes", failures)

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the rule form warns about a bad threshold and the CRUD works end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
