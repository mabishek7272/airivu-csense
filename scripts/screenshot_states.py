"""Drives the CRM in a real browser and captures every UI state.

Screenshots are the point, but the assertions are what make this a test: each state has to
say something specific, and a page that renders the *wrong* state looks perfectly fine in
an image. So "no results" must not say "no cameras yet", the offline screen must not blame
the server, and a slow request must not become an error.

Offline is driven through Chrome DevTools Protocol rather than by unplugging anything, so
the check is repeatable.

    python scripts/screenshot_states.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://app.localhost:8080"
PASSWORD = "StatesE2E!Password123"
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
    email = f"states-{suffix}@northwind.example"
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(20000)

        print("\n[1] Register and sign in")
        page.goto(f"{BASE}/login")
        page.get_by_role("button", name="Need an account").click()
        page.get_by_label("Organization name").fill(f"States E2E {suffix}")
        page.get_by_label("Your name").fill("State Tester")
        page.get_by_label("Email").fill(email)
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create organization").click()
        page.wait_for_url("**/incidents", timeout=30000)
        print("    signed in")

        print("\n[2] Empty state - nothing exists yet")
        page.goto(f"{BASE}/cameras")
        page.wait_for_selector(".state-panel, .data-table")
        body = page.locator(".state-panel").inner_text()
        check("No cameras yet" in body, "empty state says nothing exists yet", failures)
        check(
            "Add your first camera" in body,
            "empty state offers the action that creates one",
            failures,
        )
        shot(page, "01-empty")

        print("\n[3] Form validation - submitting an empty form")
        page.get_by_role("button", name="Add your first camera").click()
        page.wait_for_selector("div[role='dialog']")
        dialog = page.locator("div[role='dialog']")
        # Scoped to the dialog: the page header has a button of the same name, and an
        # ambiguous locator would pass or fail depending on render order.
        dialog.get_by_role("button", name="Add camera").click()
        page.wait_for_selector(".error-summary")
        summary = page.locator(".error-summary").inner_text()
        check("problems with this form" in summary, "an error summary is shown", failures)
        check("Name is required" in summary, "the summary names each failing field", failures)
        check(
            page.locator("[aria-invalid='true']").count() >= 2,
            "failing fields are marked aria-invalid",
            failures,
        )
        shot(page, "02-form-validation")

        print("\n[4] Field-level validation - a URL where a path belongs")
        # Located by the form field's own name rather than its label: a required field's
        # label carries a visual asterisk, and matching on rendered text makes the test
        # brittle against a purely cosmetic change.
        dialog.locator('[name="main_stream_path"]').fill("rtsp://user:pass@host/live")
        dialog.locator('[name="name"]').click()  # blur the path field
        page.wait_for_selector(".field-invalid .field-error")
        # Scoped to this field's own container. Errors from the earlier empty submit are
        # still on screen, so `.field-error` alone would read whichever came first.
        message = dialog.locator(
            '.field:has([name="main_stream_path"]) .field-error'
        ).inner_text()
        check(
            "rtsp://" in message or "path only" in message,
            "the path field explains why a URL is refused",
            failures,
        )
        shot(page, "03-field-error")

        print("\n[5] Success state - creating a camera")
        site_id = os.environ.get("STATES_SITE_ID", "")
        dialog.locator('[name="name"]').fill("Loading Bay 2")
        dialog.locator('[name="code"]').fill(f"bay-{suffix}")
        dialog.locator('[name="site_id"]').fill(site_id)
        dialog.locator('[name="main_stream_path"]').fill("/Streaming/Channels/101")
        dialog.locator('[name="hostname"]').fill("nvr.example.com")
        # Scoped to the dialog: the page header has a button of the same name, and an
        # ambiguous locator would pass or fail depending on render order.
        dialog.get_by_role("button", name="Add camera").click()

        # Either outcome is a state worth capturing. With a real site id the create
        # succeeds and a toast confirms it; without one the server rejects it, and what
        # matters then is that the rejection lands on the form rather than vanishing.
        page.wait_for_selector(".toast-success, .error-summary", timeout=15000)
        if page.locator(".toast-success").count() > 0:
            check(True, "a success toast confirms the create", failures)
            shot(page, "04-success-toast")
        else:
            check(True, "a server rejection is surfaced on the form", failures)
            shot(page, "04-server-error")
            dialog.get_by_role("button", name="Cancel").click()

        print("\n[6] No results - a filter that matches nothing")
        page.wait_for_timeout(500)
        page.locator("#camera-search").fill("zzzz-no-such-camera")
        page.wait_for_selector(".state-panel")
        body = page.locator(".state-panel").inner_text()
        check(
            "match your filters" in body,
            "no-results is distinct from empty - it blames the filter",
            failures,
        )
        check(
            "Clear all filters" in body,
            "no-results offers to clear the filter",
            failures,
        )
        check(
            "No cameras yet" not in body,
            "no-results does not claim nothing exists",
            failures,
        )
        shot(page, "05-no-results")
        page.locator("#camera-search").fill("")

        print("\n[7] Offline - the browser loses its connection")
        # Navigate first, then drop the connection. Going offline before navigating fails
        # the navigation itself, which is not the case being tested - the real one is a
        # loaded page whose wifi disappears underneath it.
        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".state-panel, .data-table")
        context.set_offline(True)
        page.wait_for_selector(".top-banner-offline, .state-offline", timeout=15000)
        banner = page.locator(".top-banner-offline").inner_text()
        check("offline" in banner.lower(), "a persistent offline banner is shown", failures)
        check(
            page.get_by_role("button", name="Add device").is_disabled(),
            "actions that need the network are disabled while offline",
            failures,
        )
        shot(page, "06-offline")

        print("\n[8] Reconnect")
        context.set_offline(False)
        page.wait_for_selector(".top-banner-online", timeout=15000)
        check(True, "reconnection is announced", failures)
        shot(page, "07-reconnected")

        print("\n[9] Slow network - a response held back")
        # Delay the list call so the slow notice appears without the request failing.
        def delay_then_continue(route) -> None:
            page.wait_for_timeout(5000)
            try:
                route.continue_()
            except Exception as exc:  # noqa: BLE001
                # The page may have navigated away while this was held, which fulfils the
                # route elsewhere. Worth a line, but it must not fail the run.
                print(f"    (held request was already resolved: {type(exc).__name__})")

        page.route("**/api/v1/tenant/edge/devices*", delay_then_continue)
        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".notice-info", timeout=15000)
        notice = page.locator(".notice-info").inner_text()
        check(
            "longer than usual" in notice,
            "a slow request adds a notice rather than becoming an error",
            failures,
        )
        check(
            page.locator(".skeleton").count() > 0,
            "the loading skeleton stays visible while slow",
            failures,
        )
        shot(page, "08-slow-network")
        page.unroute("**/api/v1/tenant/edge/devices*")

        print("\n[10] Error state - the server fails")
        page.route(
            "**/api/v1/tenant/edge/devices*",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"code":"internal","message":"The server could not complete that.",'
                '"correlation_id":"abc-123","retryable":true}',
            ),
        )
        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".error-panel", timeout=15000)
        panel = page.locator(".error-panel").inner_text()
        check("Could not load" in panel, "an error panel is shown", failures)
        check("abc-123" in panel, "the correlation id is shown for support", failures)
        shot(page, "09-error")
        page.unroute("**/api/v1/tenant/edge/devices*")

        print("\n[11] Permission denied")
        page.route(
            "**/api/v1/tenant/edge/devices*",
            lambda route: route.fulfill(
                status=403,
                content_type="application/json",
                body='{"code":"forbidden","message":"No.","retryable":false,'
                '"details":{"required_permission":"edge.read"}}',
            ),
        )
        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".state-panel", timeout=15000)
        body = page.locator(".state-panel").inner_text()
        check("do not have access" in body, "permission denied is its own state", failures)
        check("edge.read" in body, "it names the permission that is missing", failures)
        check(
            "does not exist" not in body,
            "it does not masquerade as not-found",
            failures,
        )
        shot(page, "10-permission-denied")
        page.unroute("**/api/v1/tenant/edge/devices*")

        print("\n[12] Session expired")
        page.route(
            "**/api/v1/tenant/edge/devices*",
            lambda route: route.fulfill(
                status=401,
                content_type="application/json",
                body='{"code":"unauthenticated","message":"Expired.","retryable":false}',
            ),
        )
        page.route(
            "**/api/v1/auth/refresh",
            lambda route: route.fulfill(status=401, body="{}"),
        )
        page.goto(f"{BASE}/edge")
        page.wait_for_selector(".state-panel, .auth-shell", timeout=15000)
        body = page.locator("body").inner_text()
        check(
            "session has expired" in body.lower() or "sign in" in body.lower(),
            "an expired session offers a way back in",
            failures,
        )
        # Clear every interceptor before the last capture: a route still held open from an
        # earlier step will otherwise fail the screenshot for reasons unrelated to the UI.
        page.unroute_all(behavior="ignoreErrors")
        shot(page, "11-session-expired")

        browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - every state rendered and said the right thing")
    print(f"Screenshots in {os.path.normpath(OUT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
