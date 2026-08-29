"""Draws a zone in a browser, with the mouse and then with the keyboard only.

The keyboard path is the reason this test exists. A drag-only polygon editor excludes
anyone who cannot use a pointer, and this is a tool people are required to operate — so
the keyboard route is not a nicety to be checked by eye once and forgotten.

It also pins down the two guards that stop a silently-dead zone being saved: fewer than
three points, and three points in a line, which encloses no area and therefore never
overlaps anything enough to fire.

    python scripts/e2e_zone_editor.py [--headed]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "ZoneE2E!Password123"
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


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def click_canvas(page, fx: float, fy: float) -> None:
    """Clicks at a fraction of the drawing canvas."""
    box = page.locator(".polygon-canvas").bounding_box()
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 950}).new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register and create a site")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"Zone E2E {suffix}")
        page.get_by_label("Your name").fill("Zone Tester")
        page.get_by_label("Email").fill(f"zones-{suffix}@northwind.example")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)

        page.goto(f"{BASE}/sites")
        page.wait_for_selector(".state-panel")
        page.get_by_role("button", name="Add your first site").click()
        page.wait_for_selector("div[role='dialog']")
        site_dialog = page.locator("div[role='dialog']")
        site_dialog.locator('[name="name"]').fill("Warehouse Mumbai")
        site_dialog.locator('[name="code"]').fill(f"mumbai-{suffix}")
        site_dialog.get_by_role("button", name="Add site").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        print("    site created")

        print("\n[2] Zones starts empty and explains what a zone is for")
        page.goto(f"{BASE}/zones")
        page.wait_for_selector(".state-panel")
        body = page.locator(".state-panel").inner_text()
        check("No zones yet" in body, "the empty state is specific to zones", failures)
        check(
            "whole frame" in body,
            "it explains the consequence of having none",
            failures,
        )
        shot(page, "19-zones-empty")

        print("\n[3] Too few points is refused before it reaches the server")
        page.get_by_role("button", name="Draw your first zone").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        dialog.locator('[name="name"]').fill("Restricted dock")
        click_canvas(page, 0.30, 0.35)
        click_canvas(page, 0.70, 0.35)
        dialog.get_by_role("button", name="Create zone").click()
        page.wait_for_selector(".error-summary")
        summary = page.locator(".error-summary").inner_text()
        check("at least 3 points" in summary, "two points is refused, with the reason", failures)
        shot(page, "20-zone-too-few-points")

        print("\n[4] A shape with no area is refused, and says why it matters")
        # Three points in a straight line: geometrically valid, operationally dead.
        click_canvas(page, 0.50, 0.35)
        page.wait_for_timeout(300)
        dialog.get_by_role("button", name="Create zone").click()
        page.wait_for_selector(".error-summary")
        summary = page.locator(".error-summary").inner_text()
        check(
            "no area" in summary or "almost no area" in summary,
            "collinear points are caught rather than saved as a dead zone",
            failures,
        )
        check(
            "trigger a rule" in summary,
            "the message explains the consequence, not just the geometry",
            failures,
        )
        shot(page, "21-zone-no-area")

        print("\n[5] Draw a real polygon with the mouse")
        page.get_by_role("button", name="Start over").click()
        for fx, fy in ((0.30, 0.30), (0.75, 0.30), (0.75, 0.80), (0.30, 0.80)):
            click_canvas(page, fx, fy)
            page.wait_for_timeout(120)
        handles = page.locator(".polygon-handle")
        check(handles.count() == 4, "four points were placed", failures)
        status = page.locator(".polygon-status").inner_text()
        check("% of the frame" in status, "the covered area is shown while drawing", failures)
        shot(page, "22-zone-drawn")

        print("\n[6] Move a point with the keyboard only")
        first = handles.first
        first.focus()
        label_before = first.get_attribute("aria-label")
        for _ in range(5):
            page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowDown")
        label_after = first.get_attribute("aria-label")
        check(
            label_before != label_after,
            "arrow keys move a point without a pointer",
            failures,
        )
        check(
            "percent across" in (label_after or ""),
            "the point announces its position in terms that can be read aloud",
            failures,
        )
        announcement = page.locator("[role='status']").last.inner_text()
        check(
            "percent" in announcement,
            "each keyboard move is announced to a screen reader",
            failures,
        )
        print(f"      before: {label_before[:52]}...")
        print(f"      after : {label_after[:52]}...")

        print("\n[7] Add and remove a point with the keyboard")
        page.keyboard.press("Enter")
        check(handles.count() == 5, "Enter inserts a point after the focused one", failures)
        page.locator(".polygon-handle").nth(1).focus()
        page.keyboard.press("Delete")
        check(handles.count() == 4, "Delete removes the focused point", failures)
        shot(page, "23-zone-keyboard-edited")

        print("\n[8] Save it")
        dialog.get_by_role("button", name="Create zone").click()
        page.wait_for_selector(".toast-success", timeout=15000)
        page.wait_for_selector(".data-table")
        row = page.locator(".data-table tbody tr").first.inner_text()
        check("% of frame" in row, "the listing shows how much of the frame it covers", failures)
        check("no rules" in row, "the listing shows nothing depends on it yet", failures)
        check(
            page.locator(".zone-thumb").count() == 1,
            "the listing renders the shape, not just a point count",
            failures,
        )
        shot(page, "24-zone-created")

        print("\n[9] The stored polygon is normalised, not pixels")
        # Read from the database rather than through the page. The access token is held in
        # memory by the app, not in a cookie, so a `fetch` from page context has no
        # Authorization header and would 401 - which would look like an empty polygon
        # rather than an auth problem.
        stored = psql(
            "SELECT geometry_json FROM zones WHERE name = 'Restricted dock' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        polygon = json.loads(stored).get("polygon", []) if stored else []
        in_range = bool(polygon) and all(
            0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0 for x, y in polygon
        )
        check(in_range, "every stored point is a 0..1 fraction of the frame", failures)
        print(f"      stored: {[[round(x, 3), round(y, 3)] for x, y in polygon]}")

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - a zone can be drawn with a mouse or a keyboard, and dead shapes are refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
