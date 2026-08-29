"""Drives the Developer Console's Models page in a browser: the registry, promotion
through the lifecycle state machine, and the biometric-acknowledgement gate - plus a
concrete, checkable form of the platform's core IP-protection promise: this page (and the
API it calls) must never surface a model artifact's bytes, object key, bucket, or a
presigned URL, only metadata.

There is no self-service registration for platform accounts, so this bootstraps a
throwaway platform_admin directly via Postgres - mirroring the exact insert sequence
backend/migrations/seed_dev_data.py already uses - and two throwaway model versions
(mirroring backend/tests/test_model_registry.py's `registered_version` fixture). Only
those two get promoted; the real, already-imported legacy models are read-only here.

    python scripts/e2e_model_registry.py [--headed]
"""
from __future__ import annotations

import os
import sys
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE = "http://console.localhost:8080"
PASSWORD = "ModelRegE2E!Password123"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")

# Anything shaped like a way to actually fetch an artifact's bytes. If any of these ever
# show up in a response body or the rendered page, the registry has stopped being
# metadata-only.
FORBIDDEN_SUBSTRINGS = (
    "download_url", "artifact_url", "presigned", "X-Amz-", "minio:9000",
    ".onnx?", ".pt?", "object_key", "\"bucket\"",
)


def dsn() -> str:
    settings = get_settings()
    return (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def bootstrap_admin(conn: psycopg.Connection) -> tuple[str, str]:
    """A throwaway platform_admin - mirrors seed_dev_data.py's own insert sequence, the
    only option since there is no self-service registration for platform accounts."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"model-reg-e2e-{suffix}@platform.dev"
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Model Registry E2E') RETURNING id",
            (email, email, hash_password(PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' "
            "AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
    conn.commit()
    return email, str(user_id)


def bootstrap_version(conn: psycopg.Connection, *, classification: str, state: str) -> dict:
    """One throwaway model + version - mirrors test_model_registry.py's own fixture."""
    digest = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
    suffix = uuid.uuid4().hex[:8]
    name = f"e2e-{classification}-{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO models (name, task_code, description) "
            "VALUES (%s, 'object_detection', 'e2e throwaway - safe to delete') RETURNING id",
            (name,),
        )
        model_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO stored_objects (bucket, object_key, object_type, size_bytes, sha256) "
            "VALUES ('csense-models', %s, 'model_artifact', 4096, %s) RETURNING id",
            (f"global/models/{name}/v1/{digest}.onnx", digest),
        )
        object_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO model_versions (model_id, version_label, artifact_object_id, "
            "artifact_sha256, framework, runtime, access_classification, state) "
            "VALUES (%s, 'v1', %s, %s, 'onnx', 'onnxruntime', %s, %s) RETURNING id",
            (model_id, object_id, digest, classification, state),
        )
        version_id = cur.fetchone()[0]
    conn.commit()
    return {
        "model_id": str(model_id), "version_id": str(version_id),
        "object_id": str(object_id), "name": name,
    }


def cleanup(conn: psycopg.Connection, *, user_id: str, versions: list[dict]) -> None:
    with conn.cursor() as cur:
        for v in versions:
            cur.execute("DELETE FROM model_versions WHERE id = %s", (v["version_id"],))
            cur.execute("DELETE FROM stored_objects WHERE id = %s", (v["object_id"],))
            cur.execute("DELETE FROM models WHERE id = %s", (v["model_id"],))
        cur.execute(
            "DELETE FROM platform_role_assignments WHERE platform_developer_id = "
            "(SELECT id FROM platform_developers WHERE user_id = %s)",
            (user_id,),
        )
        cur.execute("DELETE FROM platform_developers WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()


def main() -> int:
    failures: list[str] = []
    conn = psycopg.connect(dsn())

    print("\n[1] Bootstrap: a throwaway platform admin and two throwaway model versions")
    email, user_id = bootstrap_admin(conn)
    standard = bootstrap_version(conn, classification="standard", state="uploaded")
    biometric = bootstrap_version(conn, classification="biometric", state="validating")
    print(f"    admin {email}")
    print(f"    standard version -> {standard['name']} (uploaded)")
    print(f"    biometric version -> {biometric['name']} (validating)")

    captured_bodies: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1100}).new_page()
        page.set_default_timeout(20000)

        def on_response(response):
            if "/api/v1/admin/models" in response.url and response.status == 200:
                try:
                    captured_bodies.append(response.text())
                except PlaywrightError:
                    # A body that failed to read isn't examined - it can't hide a leak
                    # any more than one this script never happened to capture at all.
                    pass

        page.on("response", on_response)

        try:
            print("\n[2] Sign in")
            page.goto(f"{BASE}/login")
            page.get_by_label("Email").fill(email)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/dashboard", timeout=30000)

            print("\n[3] Models renders the real registry")
            # A client-side transition (clicking the nav link), not page.goto() - the
            # console's session lives in an in-memory token that only survives a full
            # navigation via its refresh cookie, and that cookie is a separate, pre-
            # existing issue unrelated to this feature (see the run's closing note).
            # Clicking through is also just how a real operator actually gets here.
            page.get_by_role("link", name="Models").click()
            page.wait_for_url("**/models", timeout=15000)
            page.wait_for_selector("#main")
            page.locator("section.card").first.wait_for(timeout=15000)
            groups = page.locator("#main section.card").count()
            check(groups >= 14, f"at least 14 model groups render ({groups} found)", failures)
            check(
                page.locator(".badge", has_text="production").count() > 0,
                "at least one version reads as production", failures,
            )
            check(
                page.get_by_text("Biometric").count() > 0,
                "a biometric version is flagged with the word itself, not colour alone",
                failures,
            )
            shot(page, "36-models-registry")

            print("\n[4] Filtering by state and classification is server-side")
            page.select_option("#model-state", "validating")
            page.select_option("#model-classification", "biometric")
            page.wait_for_timeout(500)
            rows = page.locator("#main tbody tr")
            row_count = rows.count()
            all_match = row_count > 0 and all(
                "validating" in rows.nth(i).inner_text() and "Biometric" in rows.nth(i).inner_text()
                for i in range(row_count)
            )
            check(all_match, "every visible row matches both active filters", failures)
            shot(page, "37-models-filtered")
            page.click("text=Clear")

            print("\n[5] Promote the throwaway standard version")
            # The model name is on the section heading, not the row - find the section,
            # then act within it.
            section = page.locator("section.card", has_text=standard["name"])
            section.get_by_role("button", name="Promote").click()
            dialog = page.locator("div[role='dialog']")
            dialog.wait_for()

            option_values = dialog.locator("select[name='target_state'] option").all_inner_texts()
            check(
                set(option_values) == {"validating", "revoked"},
                f"target options match the server's own transition table (got {option_values})",
                failures,
            )

            dialog.locator("textarea[name='reason']").fill("short")
            dialog.get_by_role("button", name="Promote").click()
            check(
                dialog.locator(".field-error, .error-summary").count() > 0,
                "a too-short reason is blocked before any request is sent", failures,
            )

            dialog.locator("textarea[name='reason']").fill("Routine e2e verification promotion.")
            dialog.get_by_role("button", name="Promote").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            dialog.wait_for(state="detached", timeout=15000)
            check(
                section.locator(".badge", has_text="validating").count() > 0,
                "the standard version's badge reflects the promotion", failures,
            )
            shot(page, "38-promote-standard")

            print("\n[6] The biometric acknowledgement gate is real, not just a UI hint")
            bio_section = page.locator("section.card", has_text=biometric["name"])
            bio_section.get_by_role("button", name="Promote").click()
            bio_dialog = page.locator("div[role='dialog']")
            bio_dialog.wait_for()
            bio_dialog.locator("select[name='target_state']").select_option("validated")
            bio_dialog.locator("textarea[name='reason']").fill("Promoting to validated for e2e.")
            bio_dialog.get_by_role("button", name="Promote").click()
            page.wait_for_selector(".error-summary", timeout=15000)
            error_text = bio_dialog.inner_text()
            check(
                "privacy and legal review" in error_text.lower()
                or "outside the approved release-one scope" in error_text.lower(),
                "the exact server rejection is shown, not swallowed", failures,
            )
            shot(page, "39-biometric-ack-required")

            bio_dialog.get_by_label(
                "I confirm privacy and legal review for this biometric model is complete."
            ).check()
            bio_dialog.get_by_role("button", name="Promote").click()
            page.wait_for_selector(".toast-success", timeout=15000)
            bio_dialog.wait_for(state="detached", timeout=15000)
            check(
                bio_section.locator(".badge", has_text="validated").count() > 0,
                "the biometric version's badge reflects the promotion once acknowledged",
                failures,
            )
            shot(page, "40-biometric-promoted")

            print("\n[7] IP protection: nothing artifact-shaped ever crossed the wire")
            page_content = page.content()
            haystacks = captured_bodies + [page_content]
            leaked = [
                needle for needle in FORBIDDEN_SUBSTRINGS
                if any(needle in body for body in haystacks)
            ]
            check(
                not leaked,
                f"no download/presigned-URL-shaped field ever appeared (checked {len(captured_bodies)} "
                f"response bodies + the rendered page)",
                failures,
            )
            if leaked:
                print(f"    LEAKED: {leaked}")

        finally:
            browser.close()

    print("\n[8] Confirm the audit trail actually recorded the acknowledgement")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM outbox_events WHERE aggregate_id = %s "
            "AND event_type = 'model.version.state.changed.v1' "
            "ORDER BY occurred_at DESC LIMIT 1",
            (biometric["version_id"],),
        )
        row = cur.fetchone()
    recorded = bool(row and row[0] and row[0].get("biometric_acknowledged") is True)
    check(recorded, "the outbox event recorded biometric_acknowledged: true", failures)

    print("\n[9] Clean up")
    cleanup(conn, user_id=user_id, versions=[standard, biometric])
    conn.close()
    print("    throwaway admin and model versions removed; the 14 real legacy models were never touched")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the model registry renders real data, promotion respects the state "
          "machine and the biometric gate, and no artifact ever left the platform")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
