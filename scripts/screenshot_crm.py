"""Drives the Customer CRM in a real browser and captures each screen.

Verifies the app actually renders and works, rather than only that it compiles. Run
scripts/seed_demo_tenant.py first and pass the credentials it printed:

    python scripts/screenshot_crm.py <email> <password> [output_dir]
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = os.environ.get("CRM_BASE", "http://app.localhost:8080")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    email, password = sys.argv[1], sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "crm-screenshots"
    os.makedirs(out_dir, exist_ok=True)

    failures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 940})

        # Surface console errors: a page can render and still be broken underneath.
        # The 401 from the silent-refresh attempt before login is expected — there is no
        # session cookie yet — so it is not a failure.
        console_errors: list[str] = []
        page.on(
            "console",
            lambda m: console_errors.append(m.text)
            if m.type == "error" and "401" not in m.text
            else None,
        )

        page.goto(f"{BASE}/login", wait_until="networkidle")
        page.screenshot(path=os.path.join(out_dir, "1-login.png"))

        page.fill("#email", email)
        page.fill("#password", password)
        page.click("button[type=submit]")
        page.wait_for_url("**/incidents", timeout=30_000)
        page.wait_for_selector("article.card", timeout=30_000)
        page.wait_for_timeout(800)
        page.screenshot(path=os.path.join(out_dir, "2-incidents.png"), full_page=True)

        incident_count = page.locator("article.card").count()
        print(f"incidents listed: {incident_count}")
        if incident_count == 0:
            failures.append("incident inbox rendered no rows")

        # Detections feed - the screen that must show snapshot + boundary + location.
        page.click("a[href='/detections']")
        page.wait_for_selector("article.detection-card", timeout=30_000)
        # Wait for at least one evidence image to actually load, not just the <img> tag.
        page.wait_for_function(
            "() => [...document.querySelectorAll('.thumb img')].some(i => i.complete && i.naturalWidth > 0)",
            timeout=30_000,
        )
        page.wait_for_timeout(600)
        page.screenshot(path=os.path.join(out_dir, "3-detections.png"), full_page=True)

        images = page.evaluate(
            "() => [...document.querySelectorAll('.thumb img')]"
            ".map(i => ({ loaded: i.complete && i.naturalWidth > 0, alt: i.alt }))"
        )
        loaded = sum(1 for i in images if i["loaded"])
        print(f"evidence images: {loaded}/{len(images)} loaded")
        if loaded == 0:
            failures.append("no evidence snapshots rendered")
        if any(not i["alt"] for i in images):
            failures.append("an evidence image has no alt text")

        # Incident detail, reached the way a user would. Pick an incident that is
        # actually open rather than whichever sorts first - otherwise a previous run that
        # acknowledged one silently changes this run's preconditions.
        page.click("a[href='/incidents']")
        page.wait_for_selector("article.card a[href^='/incidents/']", timeout=30_000)
        open_card = page.locator("article.card").filter(
            has=page.locator("span.badge", has_text="open")
        ).first
        if open_card.count() == 0:
            failures.append("no open incident available to work")
            open_card = page.locator("article.card").first
        open_card.locator("a[href^='/incidents/']").first.click()
        page.wait_for_selector("ol.timeline li", timeout=30_000)
        page.wait_for_timeout(900)
        page.screenshot(path=os.path.join(out_dir, "4-incident-detail.png"), full_page=True)

        timeline_entries = page.locator("ol.timeline li").count()
        print(f"timeline entries: {timeline_entries}")
        if timeline_entries == 0:
            failures.append("incident detail showed no history")

        # Work the incident, proving the action buttons are wired to the API.
        acknowledge = page.get_by_role("button", name="Acknowledge")
        if acknowledge.count() > 0:
            acknowledge.first.click()
            # Wait for the badge itself to change rather than guessing at a delay — a
            # fixed sleep either flakes or hides a slow path.
            try:
                page.wait_for_function(
                    "() => [...document.querySelectorAll('span.badge')]"
                    ".some(b => b.textContent.includes('acknowledged'))",
                    timeout=20_000,
                )
            except PlaywrightTimeout:
                failures.append("acknowledging did not update the status badge")
            page.screenshot(path=os.path.join(out_dir, "5-after-acknowledge.png"), full_page=True)
            badges = [b.strip() for b in page.locator("span.badge").all_inner_texts() if b.strip()]
            print(f"badges after acknowledge: {badges[:3]}")
        else:
            failures.append("no Acknowledge action offered on an open incident")

        # Narrow viewport: operators use tablets on a floor walk.
        page.set_viewport_size({"width": 420, "height": 900})
        page.goto(f"{BASE}/detections", wait_until="networkidle")
        page.wait_for_selector("article.detection-card", timeout=30_000)
        page.wait_for_timeout(1200)
        page.screenshot(path=os.path.join(out_dir, "6-mobile-detections.png"), full_page=True)

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )
        if overflow:
            failures.append("page scrolls horizontally at 420px wide")

        if console_errors:
            print(f"console errors: {console_errors[:3]}")
            failures.append(f"{len(console_errors)} console error(s)")

        browser.close()

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll checks passed. Screenshots in {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
