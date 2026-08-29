"""Proves the `validating -> validated` promotion gate is real, against the real running
admin-api - not just that the endpoints exist.

Bootstraps a throwaway platform_admin and a throwaway model version already in
`validating` state (mirroring `scripts/e2e_model_registry.py`'s own fixture pattern
exactly), then drives the real HTTP API: promotion is refused with no validation run
recorded, still refused after a `failed` one, and succeeds only after a `passed` one is
recorded through the same `POST .../validation-runs` endpoint
`scripts/run_model_validation.py` uses for real. Also confirms the gate is scoped to
exactly the one transition it's meant for - `uploaded -> validating` (used by this very
bootstrap, and by `e2e_model_registry.py`'s own promotion test) is untouched.

    python scripts/e2e_model_validation_gate.py
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password

ADMIN_API = "http://localhost:8080"
PASSWORD = "ValidationGateE2E!Password123"


def dsn() -> str:
    settings = get_settings()
    return (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def bootstrap_admin(conn: psycopg.Connection) -> tuple[str, str]:
    """Mirrors e2e_model_registry.py's own bootstrap_admin - the only option, since
    there is no self-service registration for platform accounts."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"validation-gate-e2e-{suffix}@platform.dev"
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Validation Gate E2E') RETURNING id",
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


def bootstrap_version(conn: psycopg.Connection, *, state: str) -> dict:
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    suffix = uuid.uuid4().hex[:8]
    name = f"e2e-validation-gate-{suffix}"
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
            "VALUES (%s, 'v1', %s, %s, 'onnx', 'onnxruntime', 'standard', %s) RETURNING id",
            (model_id, object_id, digest, state),
        )
        version_id = cur.fetchone()[0]
    conn.commit()
    return {"model_id": str(model_id), "version_id": str(version_id), "object_id": str(object_id), "name": name}


def cleanup(conn: psycopg.Connection, *, user_id: str, versions: list[dict]) -> None:
    with conn.cursor() as cur:
        for v in versions:
            cur.execute("DELETE FROM model_validation_runs WHERE model_version_id = %s", (v["version_id"],))
            cur.execute("DELETE FROM model_versions WHERE id = %s", (v["version_id"],))
            cur.execute("DELETE FROM stored_objects WHERE id = %s", (v["object_id"],))
            cur.execute("DELETE FROM models WHERE id = %s", (v["model_id"],))
        cur.execute(
            "DELETE FROM platform_role_assignments WHERE platform_developer_id = "
            "(SELECT id FROM platform_developers WHERE user_id = %s)",
            (user_id,),
        )
        cur.execute(
            "DELETE FROM platform_developers WHERE user_id = %s", (user_id,)
        )
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "console.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{ADMIN_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def promote(token, version_id, target_state, expect):
    return api(
        f"/api/v1/admin/model-versions/{version_id}/promote",
        {"target_state": target_state, "reason": "e2e validation gate check"},
        token, expect=expect,
    )


def main() -> int:
    failures: list[str] = []
    conn = psycopg.connect(dsn())
    email, user_id = bootstrap_admin(conn)
    versions: list[dict] = []

    try:
        _, auth = api("/api/v1/admin/auth/login", {"email": email, "password": PASSWORD})
        token = auth["access_token"]

        step(1, "The gate is scoped to validating -> validated only")
        uploaded = bootstrap_version(conn, state="uploaded")
        versions.append(uploaded)
        status, _ = promote(token, uploaded["version_id"], "validating", expect=(200,))
        check(status == 200, "uploaded -> validating still works with no validation run", failures)

        step(2, "validating -> validated is refused with no run recorded")
        validating = bootstrap_version(conn, state="validating")
        versions.append(validating)
        status, body = promote(token, validating["version_id"], "validated", expect=(422,))
        check(status == 422, "refused (422) with no validation run", failures)
        check(body.get("code") == "validation_run_required", "the refusal names itself clearly", failures)

        step(3, "Still refused after a *failed* run")
        api(
            f"/api/v1/admin/model-versions/{validating['version_id']}/validation-runs",
            {
                "suite_version": "e2e-gate-check", "environment": "e2e", "status": "failed",
                "metrics": {"recall": 0.1}, "thresholds": {"min_recall": 0.8},
            },
            token, expect=(201,),
        )
        status, body = promote(token, validating["version_id"], "validated", expect=(422,))
        check(status == 422, "still refused (422) after a failed run", failures)

        step(4, "Succeeds once a *passed* run is recorded")
        api(
            f"/api/v1/admin/model-versions/{validating['version_id']}/validation-runs",
            {
                "suite_version": "e2e-gate-check", "environment": "e2e", "status": "passed",
                "metrics": {"recall": 1.0}, "thresholds": {"min_recall": 0.8},
            },
            token, expect=(201,),
        )
        status, out = promote(token, validating["version_id"], "validated", expect=(200,))
        check(status == 200, "promotion succeeds with a passed run on record", failures)
        check(out.get("state") == "validated", "the version actually landed in 'validated'", failures)

        step(5, "The list endpoint surfaces the latest run's summary")
        _, models = api("/api/v1/admin/models", token=token, method="GET")
        row = next((m for m in models if m["id"] == validating["version_id"]), None)
        check(row is not None, "the promoted version is listed", failures)
        check(
            row is not None and row.get("latest_validation_status") == "passed",
            "GET /models reflects the latest (passed) run, not the earlier failed one", failures,
        )

        step(6, "Clean up")
        cleanup(conn, user_id=user_id, versions=versions)
        print("    throwaway versions and admin removed")

    finally:
        conn.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the validating -> validated promotion gate is real, not just a schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
