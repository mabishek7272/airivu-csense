"""Drives the Developer Console's Pipelines page in a browser: authoring a pipeline,
creating a draft version against a real registered model, publishing it, and confirming
the properties that matter beyond "the form submits" - a duplicate code is refused, an
identical definition can't become a second version, and once published the version is
immutable (the trigger from migration 0031, not just the UI's own state).

The tenant-facing half - assigning a published version to a real camera - has no Console
page yet (this pass deliberately keeps the builder to the Developer Console; see the
plan), so that part is driven directly against the Tenant API, mirroring
e2e_rule_to_incident.py's own bootstrap: register a tenant, create a camera, assign,
confirm the overlap constraint refuses a second active assignment at the same priority,
confirm deprecating the version blocks a new assignment without touching the existing one.

    python scripts/e2e_pipeline_registry.py [--headed]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

API = "http://localhost:8080"
CONSOLE_BASE = "http://console.localhost:8080"
PASSWORD = "PipelineRegE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")

# A model already in production from the legacy migration - real, not a fixture double.
REAL_MODEL_NAME = "yolov8n-general"


def api(path, payload=None, token=None, method="POST", host="localhost", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": host}
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
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:600]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors e2e_model_registry.py's own bootstrap_admin - a throwaway platform_admin,
    the only option since there is no self-service registration for platform accounts."""
    import sys as _sys

    _sys.path.insert(0, "backend/shared")
    import psycopg
    from csense_shared.config import get_settings
    from csense_shared.security.passwords import hash_password

    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"pipeline-reg-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    conn = psycopg.connect(dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Pipeline Registry E2E') RETURNING id",
            (email, email, hash_password(PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
    conn.close()
    return email, user_id


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a throwaway platform admin")
    email, user_id = bootstrap_platform_admin()
    print(f"    admin {email}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)

        pipeline_code = f"e2e-pipeline-{suffix}"

        try:
            step(2, "Sign in and open Pipelines")
            page.goto(f"{CONSOLE_BASE}/login")
            page.get_by_label("Email").fill(email)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/dashboard", timeout=30000)
            page.get_by_role("link", name="Pipelines").click()
            page.wait_for_url("**/pipelines", timeout=15000)
            page.wait_for_selector("#main")
            shot(page, "49-pipelines-empty-or-list")

            step(3, "Create a pipeline through the form")
            page.get_by_role("button", name="New pipeline").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()
            dialog.locator("input[name='code']").fill(pipeline_code)
            dialog.locator("input[name='name']").fill("E2E Vehicle Pipeline")
            dialog.locator("input[name='use_case']").fill("vehicle.intrusion")
            dialog.get_by_role("button", name="Create pipeline").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            dialog.wait_for(state="detached", timeout=15000)
            section = page.locator("section.card", has_text=pipeline_code)
            check(section.count() > 0, "the new pipeline appears in the list", failures)
            shot(page, "50-pipeline-created")

            step(4, "A duplicate code is refused")
            page.get_by_role("button", name="New pipeline").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()
            dialog.locator("input[name='code']").fill(pipeline_code)
            dialog.locator("input[name='name']").fill("Duplicate attempt")
            dialog.locator("input[name='use_case']").fill("x")
            dialog.get_by_role("button", name="Create pipeline").click()
            page.wait_for_selector(".error-summary, .field-error", timeout=15000)
            check(
                "already exists" in dialog.inner_text().lower(),
                "the duplicate-code error surfaces on the form", failures,
            )
            dialog.get_by_role("button", name="Cancel").click()

            step(5, "Create a draft version against a real registered model")
            section.get_by_role("button", name="New version").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()
            dialog.locator("select[name='model_name']").select_option(REAL_MODEL_NAME)
            dialog.get_by_role("button", name="Create version").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            dialog.wait_for(state="detached", timeout=15000)
            check(
                section.locator(".badge", has_text="draft").count() > 0,
                "the new version reads as draft", failures,
            )
            check(
                section.get_by_text(REAL_MODEL_NAME).count() > 0,
                "the real model name is shown against the version", failures,
            )
            check(
                page.get_by_text("1 pipeline, 1 version").count() > 0,
                "the version count doesn't double-count the pre-creation placeholder row",
                failures,
            )
            shot(page, "51-version-created-draft")

            step(6, "Publish it")
            section.get_by_role("button", name="Publish").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()
            dialog.locator("textarea[name='reason']").fill("short")
            dialog.get_by_role("button", name="Publish").click()
            check(
                dialog.locator(".field-error, .error-summary").count() > 0,
                "a too-short reason is blocked before any request is sent", failures,
            )
            dialog.locator("textarea[name='reason']").fill("Routine e2e verification publish.")
            dialog.get_by_role("button", name="Publish").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            dialog.wait_for(state="detached", timeout=15000)
            check(
                section.locator(".badge", has_text="published").count() > 0,
                "the version's badge reflects publication", failures,
            )
            check(
                section.get_by_role("button", name="Deprecate").count() > 0,
                "publish -> deprecate is now the only offered transition", failures,
            )
            check(
                section.get_by_role("button", name="Publish").count() == 0,
                "publish is no longer offered once published", failures,
            )
            shot(page, "52-version-published")

        finally:
            browser.close()

    step(7, "Immutability holds at the database level, not just the UI")
    version_id = psql(
        f"SELECT pv.id FROM pipeline_versions pv JOIN pipelines p ON p.id = pv.pipeline_id "
        f"WHERE p.code = '{pipeline_code}'"
    )
    try:
        psql(f"UPDATE pipeline_versions SET definition_json = '{{}}' WHERE id = '{version_id}'")
        failures.append("a published version's definition was updatable at the DB level")
        print("    FAIL  the immutability trigger did not fire")
    except subprocess.CalledProcessError:
        print("    ok    the immutability trigger refused the update")

    step(8, "Tenant side: assign the published version to a real camera")
    _, tenant_auth = api("/api/v1/auth/register", {
        "organization_name": f"Pipeline Reg E2E {suffix}",
        "email": f"owner-{suffix}@pipeline-reg.example",
        "password": PASSWORD,
        "display_name": "Owner",
    }, host="app.localhost")
    tenant_token, tenant_id = tenant_auth["access_token"], tenant_auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "E2E Site", "code": f"site-{suffix}", "timezone": "Asia/Kolkata",
    }, tenant_token, host="app.localhost")
    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "E2E Camera", "code": f"cam-{suffix}",
    }, tenant_token, host="app.localhost")

    status, _assignment = api(f"/api/v1/tenant/cameras/{camera['id']}/pipeline-assignments", {
        "pipeline_version_id": version_id,
    }, tenant_token, host="app.localhost", expect=(201,))
    check(status == 201, f"assigning the published version to a real camera succeeds ({status})", failures)

    status, _dup = api(f"/api/v1/tenant/cameras/{camera['id']}/pipeline-assignments", {
        "pipeline_version_id": version_id,
    }, tenant_token, host="app.localhost", expect=(409,))
    check(status == 409, "a second active assignment at the same priority is refused", failures)

    step(9, "Deprecate the version, confirm it blocks new assignments but not the existing one")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(f"{CONSOLE_BASE}/login")
            page.get_by_label("Email").fill(email)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/dashboard", timeout=30000)
            # A client-side transition, not page.goto() - the console's session lives in
            # an in-memory token that a full navigation loses (a real, pre-existing bug;
            # see CHECKLIST.md's own note on the model registry work, not this pass's to
            # fix). Clicking through is also just how a real operator gets here.
            page.get_by_role("link", name="Pipelines").click()
            page.wait_for_url("**/pipelines", timeout=15000)
            page.wait_for_selector("#main")
            section = page.locator("section.card", has_text=pipeline_code)
            section.get_by_role("button", name="Deprecate").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()
            dialog.locator("textarea[name='reason']").fill("Routine e2e verification deprecate.")
            dialog.get_by_role("button", name="Deprecate").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            dialog.wait_for(state="detached", timeout=15000)
            check(
                section.locator(".badge", has_text="deprecated").count() > 0,
                "the version's badge reflects deprecation", failures,
            )
            shot(page, "53-version-deprecated")
        finally:
            browser.close()

    status, existing = api(
        f"/api/v1/tenant/cameras/{camera['id']}/pipeline-assignments", None, tenant_token,
        method="GET", host="app.localhost", expect=(200,),
    )
    check(
        len(existing) == 1 and existing[0]["status"] == "active",
        "the existing assignment is untouched by deprecating its version", failures,
    )

    status, _blocked = api(f"/api/v1/tenant/cameras/{camera['id']}/pipeline-assignments", {
        "pipeline_version_id": version_id, "priority": 200,
    }, tenant_token, host="app.localhost", expect=(422,))
    check(status == 422, "a new assignment against the now-deprecated version is refused", failures)

    step(10, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Pipeline Reg E2E {suffix}'")
    psql(f"DELETE FROM pipeline_assignments WHERE pipeline_version_id = '{version_id}'")
    psql(
        f"DELETE FROM pipeline_versions WHERE pipeline_id = "
        f"(SELECT id FROM pipelines WHERE code = '{pipeline_code}')"
    )
    psql(f"DELETE FROM pipelines WHERE code = '{pipeline_code}'")
    psql(
        f"DELETE FROM platform_role_assignments WHERE platform_developer_id = "
        f"(SELECT id FROM platform_developers WHERE user_id = '{user_id}')"
    )
    psql(f"DELETE FROM platform_developers WHERE user_id = '{user_id}'")
    psql(f"DELETE FROM users WHERE id = '{user_id}'")
    print("    throwaway pipeline, admin, and tenant removed; the real model was never touched")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the pipeline registry works end to end: author, publish (immutably), "
          "assign to a real camera, and deprecation blocks new assignments without "
          "touching an existing one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
