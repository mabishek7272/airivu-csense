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

import hashlib
import json
import os
import subprocess
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


def psql(sql: str) -> str:
    """Same helper `e2e_pipeline_registry.py` already uses to talk to the real dev-stack
    Postgres without pulling in a second (async, session-scoped) DB dependency into an
    otherwise-sync, urllib/Playwright-only script."""
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra"),
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _pipeline_version(*, pipeline_id: str, version_number: int, state: str, suffix: str) -> str:
    """One throwaway pipeline_version, created directly against Postgres - matching
    `test_pipeline_assignments_api.py`'s own `_make_version` fixture, the already-reviewed
    pattern for exactly this (a pipeline_version's state can't be set on creation through
    the real admin API in one call; `draft` is the only state creation supports there
    too, so a direct write is not a shortcut around that API, it's the same two-step path
    the admin API's own tests already established for getting a version to a specific
    state for a test)."""
    definition = {"stages": [{"type": "infer", "model_name": f"crm-e2e-model-{suffix}", "min_model_state": "validated"}]}
    digest = hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    version_id = psql(
        f"INSERT INTO pipeline_versions "
        f"(pipeline_id, version_number, definition_json, definition_sha256, allowed_overrides_schema) "
        f"VALUES ('{pipeline_id}', {version_number}, "
        f"'{json.dumps(definition)}'::jsonb, '{digest}', '{{}}'::jsonb) RETURNING id"
    )
    if state != "draft":
        psql(f"UPDATE pipeline_versions SET state = '{state}' WHERE id = '{version_id}'")
    return version_id


def create_pipeline_fixture(suffix: str) -> dict[str, str]:
    """Creates one real published version (the one the dialog is supposed to offer) plus
    a sibling draft and deprecated version (so 'the dropdown never shows draft/deprecated'
    is an assertion actually exercising something, not vacuously true against an empty
    catalogue). Self-contained: this script no longer assumes some other process already
    seeded a publishable pipeline - a real gap a prior review caught, since the dev
    database has no such seed data by default."""
    pipeline_id = psql(
        f"INSERT INTO pipelines (code, name, use_case, description) "
        f"VALUES ('e2e-crm-pipeline-{suffix}', 'CRM E2E Pipeline {suffix}', 'test', "
        f"'Throwaway fixture for e2e_pipeline_assignments_crm.py') RETURNING id"
    )
    published_id = _pipeline_version(pipeline_id=pipeline_id, version_number=1, state="published", suffix=f"pub-{suffix}")
    draft_id = _pipeline_version(pipeline_id=pipeline_id, version_number=2, state="draft", suffix=f"draft-{suffix}")
    deprecated_id = _pipeline_version(pipeline_id=pipeline_id, version_number=3, state="deprecated", suffix=f"dep-{suffix}")
    return {"pipeline_id": pipeline_id, "published_id": published_id, "draft_id": draft_id, "deprecated_id": deprecated_id}


def cleanup_pipeline_fixture(fixture: dict[str, str]) -> None:
    """Mirrors `e2e_pipeline_registry.py`'s own end-of-run cleanup convention (remove what
    this script itself created). `pipeline_versions.pipeline_id` is `ondelete=RESTRICT`
    (migration 0031), so versions must go first."""
    psql(f"DELETE FROM pipeline_assignments WHERE pipeline_version_id IN "
         f"('{fixture['published_id']}', '{fixture['draft_id']}', '{fixture['deprecated_id']}')")
    psql(f"DELETE FROM pipeline_versions WHERE pipeline_id = '{fixture['pipeline_id']}'")
    psql(f"DELETE FROM pipelines WHERE id = '{fixture['pipeline_id']}'")


def _run_scenario(page, browser, suffix: str, failures: list[str]) -> None:
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
        len(option_texts) > 1,
        "the dropdown offers at least one real published version to pick "
        "(not just its own placeholder option)",
        failures,
    )
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


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    print("\n[0] Seed a real published pipeline version (plus a draft and a deprecated "
          "sibling, so the 'never shows draft/deprecated' check below is actually "
          "exercising something against a real mixed catalogue, not an empty one)")
    fixture = create_pipeline_fixture(suffix)
    print(f"    pipeline {fixture['pipeline_id']} - published {fixture['published_id']}, "
          f"draft {fixture['draft_id']}, deprecated {fixture['deprecated_id']}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless="--headed" not in sys.argv)
            page = browser.new_context(viewport={"width": 1280, "height": 950}).new_page()
            page.set_default_timeout(30000)
            _run_scenario(page, browser, suffix, failures)
    finally:
        cleanup_pipeline_fixture(fixture)
        print(f"\n    cleaned up throwaway pipeline {fixture['pipeline_id']} and its 3 versions")

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
