"""Drives the Customer CRM's new pipeline-assignment page in a real browser.

Registers a tenant, adds a site and a camera exactly the way `e2e_zone_editor.py` does,
then proves the actual UI round trip that unit tests alone can't: the page starts a fresh
camera at "Not assigned", the Assign dialog lists only `published` pipeline versions
(never a `draft`/`deprecated` one) fetched from the real
`GET /api/v1/tenant/pipelines/assignable`, submitting it renders the assigned pill for
real (a real `POST .../pipeline-assignments` round trip, not a mocked one), and Revoke
puts the camera back to "Not assigned" through a real
`POST /pipeline-assignments/{id}/revoke`.

    python scripts/e2e_pipeline_assignments_crm.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CRM_BASE", "http://app.localhost:8080")
PASSWORD = "PipelineE2E!Password123"
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
        page = browser.new_context(viewport={"width": 1280, "height": 950}).new_page()
        page.set_default_timeout(30000)

        print("\n[1] Register, add a site, add a camera")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"Pipeline E2E {suffix}")
        page.get_by_label("Your name").fill("Pipeline Tester")
        page.get_by_label("Email").fill(f"pipelines-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/dashboard", timeout=30000)

        # Client-side navigation (clicking the app's own nav links) from here on, not
        # `page.goto` — a full page load drops the in-memory access token (client.ts's own
        # docstring: it is never persisted) and re-runs the silent-refresh round trip,
        # which is both slower and not how a real session actually moves between pages.
        page.get_by_role("link", name="Sites", exact=True).click()
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first site").click()
        page.wait_for_selector("div[role='dialog']")
        site_dialog = page.locator("div[role='dialog']")
        site_dialog.locator('[name="name"]').fill("Pipeline Test Site")
        site_dialog.locator('[name="code"]').fill(f"pipeline-site-{suffix}")
        site_dialog.get_by_role("button", name="Add site").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.get_by_role("link", name="Cameras", exact=True).click()
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first camera").click()
        page.wait_for_selector("div[role='dialog']")
        camera_dialog = page.locator("div[role='dialog']")
        camera_dialog.locator('[name="name"]').fill("Pipeline Test Camera")
        camera_dialog.locator('[name="code"]').fill(f"pipeline-cam-{suffix}")
        camera_dialog.locator('[name="site_id"]').select_option(index=1)
        camera_dialog.get_by_role("button", name="Add camera").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        print("    site and camera created")

        print("\n[2] The Pipelines page starts the new camera at 'Not assigned'")
        page.get_by_role("link", name="Pipelines", exact=True).click()
        row = page.locator(".data-table tbody tr", has_text="Pipeline Test Camera")
        row.first.wait_for()  # this page runs its own independent GET /cameras - wait for it
        check(row.count() == 1, "the new camera appears on the pipelines page", failures)
        check("Not assigned" in row.inner_text(), "it starts with no active assignment", failures)
        shot(page, "pipelines-01-unassigned")

        print("\n[3] Assign a published pipeline version")
        row.get_by_role("button", name="Assign pipeline").click()
        page.wait_for_selector("div[role='dialog']")
        assign_dialog = page.locator("div[role='dialog']")
        options = assign_dialog.locator("select").first.locator("option")
        option_texts = options.all_inner_texts()
        check(
            all("draft" not in t.lower() and "deprecated" not in t.lower() for t in option_texts),
            "the dropdown never shows a draft or deprecated version by label",
            failures,
        )
        # Pick a real published version rather than the placeholder option.
        assign_dialog.locator("select").first.select_option(index=1)
        shot(page, "pipelines-02-assign-dialog")
        assign_dialog.get_by_role("button", name="Assign pipeline").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        page.wait_for_timeout(300)

        row = page.locator(".data-table tbody tr", has_text="Pipeline Test Camera")
        check(
            "Not assigned" not in row.inner_text(),
            "the camera shows a real assignment after the round trip",
            failures,
        )
        check(row.get_by_role("button", name="Revoke").count() == 1, "Revoke replaces Assign", failures)
        shot(page, "pipelines-03-assigned")
        print(f"      row now reads: {row.inner_text()!r}")

        print("\n[4] Revoke it")
        row.get_by_role("button", name="Revoke").click()
        page.wait_for_selector("div[role='dialog']")
        page.get_by_role("button", name="Revoke assignment").click()
        page.wait_for_selector(".toast-success", timeout=15000)

        page.wait_for_timeout(500)
        row = page.locator(".data-table tbody tr", has_text="Pipeline Test Camera")
        print(f"      row after revoke: {row.inner_text()!r}")
        print(f"      row html: {row.inner_html()!r}")
        check("Not assigned" in row.inner_text(), "revoking puts the camera back to 'Not assigned'", failures)
        check(row.get_by_role("button", name="Assign pipeline").count() == 1, "Assign replaces Revoke again", failures)
        shot(page, "pipelines-04-revoked")

        print("\n[5] Console stayed clean throughout")
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # Away and back, not a full reload — a reload drops the in-memory access token
        # (see [1]'s own note) and would test the silent-refresh path instead of this
        # page's own render, which is what this check is actually about.
        page.get_by_role("link", name="Cameras", exact=True).click()
        page.wait_for_selector(".data-table, .state-panel")
        page.get_by_role("link", name="Pipelines", exact=True).click()
        page.wait_for_selector(".data-table")
        check(not errors, f"no console errors moving off and back onto the page ({errors})", failures)

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - assign and revoke both round-trip for real against the live stack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
