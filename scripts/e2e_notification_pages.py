"""Drives the Recipient groups and Notification policies pages in a browser.

The API side (CRUD, quiet-hours delay, the cross-tenant collision-style guards) is proven
end to end by e2e_notification_config.py. This checks the two screens built on top of it:
that a person can actually build a recipient with quiet hours and publish an escalation
ladder by clicking, not just by posting JSON.

    python scripts/e2e_notification_pages.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "NotifUiE2E!Password123"
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
        page = browser.new_context(viewport={"width": 1280, "height": 1100}).new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"Notif UI E2E {suffix}")
        page.get_by_label("Your name").fill("Notif UI Tester")
        page.get_by_label("Email").fill(f"notifui-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)

        print("\n[2] Recipient groups starts empty and explains the consequence")
        page.goto(f"{BASE}/recipient-groups")
        page.wait_for_selector(".state-panel")
        body = page.locator(".state-panel").inner_text()
        check("No recipient groups" in body, "the empty state is specific to recipient groups",
              failures)
        check("reaches no one" in body or "nobody" in body,
              "it states what happens without one, not just that none exist", failures)
        shot(page, "32-recipients-empty")

        print("\n[3] Create a group and add a member with quiet hours")
        page.get_by_role("button", name="Add your first group").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("On call")
        d.get_by_role("button", name="Add group").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.wait_for_selector(".data-table")
        page.get_by_role("button", name="Members").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.get_by_role("button", name="Add member").click()
        d.locator('[name="display_name"]').fill("Night owl")
        d.locator('[name="email"]').fill(f"owl-{suffix}@northwind.example")
        d.get_by_label("Respect quiet hours for routine alerts").check()
        d.locator('[name="quiet_start"]').fill("22:00")
        d.locator('[name="quiet_end"]').fill("06:00")
        d.get_by_role("button", name="Add recipient").click()
        d.get_by_text("Quiet 22:00").wait_for(timeout=15000)
        check(True, "the quiet-hours summary appears on the member once saved", failures)
        shot(page, "33-member-quiet-hours")
        d.get_by_role("button", name="Done").click()

        print("\n[4] Notification policies starts empty and explains the fallback")
        page.goto(f"{BASE}/notification-policies")
        page.wait_for_selector(".state-panel")
        body = page.locator(".state-panel").inner_text()
        check("No notification policies" in body, "the empty state is specific to policies",
              failures)
        check("nobody is told" in body.lower() or "falls back" in body.lower(),
              "it explains the fallback, not just that none exist", failures)
        shot(page, "34-policies-empty")

        print("\n[5] Create a policy and publish an escalation step")
        page.get_by_role("button", name="Create your first policy").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.locator('[name="name"]').fill("Dock watch")
        d.get_by_role("button", name="Create policy").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.wait_for_selector(".data-table")
        page.get_by_text("Draft").wait_for(timeout=15000)
        page.get_by_role("button", name="Escalation").click()
        page.wait_for_selector("div[role='dialog']")
        d = page.locator("div[role='dialog']")
        d.get_by_text("On call").click()  # the recipient-group chip
        d.get_by_role("button", name="Publish").click()
        d.wait_for(state="detached", timeout=15000)
        page.wait_for_selector(".toast-success", timeout=15000)

        row = page.locator(".data-table tbody tr").first
        check("1 step" in row.inner_text(), "the listing shows the published step count",
              failures)
        check("v1" in row.inner_text(), "the listing shows the version that is live", failures)
        shot(page, "35-policy-published")

        print("\n[6] Disabling the policy is reflected in the listing")
        status_pill = row.locator(".pill").last
        page.get_by_role("button", name="Disable").click()
        status_pill.get_by_text("disabled").wait_for(timeout=15000)
        check(True, "disabling is reflected once the request completes", failures)

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - recipient groups and notification policies work end to end in the browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
