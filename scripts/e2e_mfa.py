"""Proves TOTP MFA + step-up enforcement works end-to-end for real (TRD-SEC-010): a
platform admin enrolls a real TOTP secret, confirms it with a code computed the same way
a real authenticator app would, gets real hashed recovery codes, and the step-up gate on
`POST /api/v1/admin/licenses` (see `licensing.py`) actually blocks the action until a
recent verification exists - not decorative. A wrong code is refused, a recovery code is
single-use, and removing MFA itself requires a fresh step-up.

    python scripts/e2e_mfa.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password
from csense_shared.security.totp import totp_now

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "MfaE2E!Platform123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204), host="console.localhost"):
    headers = {"Content-Type": "application/json", "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
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


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors e2e_licensing.py's own bootstrap_platform_admin."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"mfa-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'MFA E2E') RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings)),
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
        conn.commit()
    return email, str(user_id)


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    failures: list[str] = []

    step(1, "Bootstrap a platform admin, not yet MFA-enrolled")
    admin_email, admin_user_id = bootstrap_platform_admin()
    _, admin_auth = api("/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD})
    token = admin_auth["access_token"]

    _, status0 = api("/api/v1/admin/auth/mfa/status", token=token, method="GET")
    check(status0 == {"enrolled": False, "recovery_codes_remaining": 0}, "starts unenrolled", failures)

    step(2, "Enroll a real TOTP secret and confirm it with a real computed code")
    _, enrolled = api("/api/v1/admin/auth/mfa/totp/enroll", token=token, expect=(201,))
    secret = enrolled["secret"]
    check(enrolled["otpauth_uri"].startswith("otpauth://totp/"), "a real otpauth:// URI is returned", failures)

    code = totp_now(secret)
    status, confirmed = api("/api/v1/admin/auth/mfa/totp/confirm", {"code": code}, token, expect=(200,))
    check(status == 200, "confirming with the real computed code succeeds", failures)
    recovery_codes = confirmed["recovery_codes"]
    check(len(recovery_codes) == 10, "10 recovery codes are issued", failures)

    _, status1 = api("/api/v1/admin/auth/mfa/status", token=token, method="GET")
    check(status1 == {"enrolled": True, "recovery_codes_remaining": 10}, "status now shows enrolled", failures)

    step(3, "Removing MFA before any step-up is refused - enrolling itself doesn't count as one")
    status, refusal = api("/api/v1/admin/auth/mfa/totp", token=token, method="DELETE", expect=(403,))
    check(status == 403, "DELETE without a recent verification is refused (403)", failures)
    check(refusal.get("code") == "step_up_required", "clear refusal code", failures)

    step(4, "Issuing a license before any step-up is refused the same way - the real gate")
    status, refusal = api(
        "/api/v1/admin/licenses",
        {"tenant_id": str(uuid.uuid4()), "plan_code": "does-not-matter"},
        token, expect=(403,),
    )
    check(status == 403, "license issuance without step-up is refused (403)", failures)
    check(refusal.get("code") == "step_up_required", "same clear refusal code", failures)

    step(5, "A wrong code is refused")
    status, _ = api("/api/v1/admin/auth/mfa/verify", {"code": "000000"}, token, expect=(401,))
    check(status == 401, "an incorrect TOTP code is refused (401)", failures)

    step(6, "A correct code verifies and actually lifts the license-issuance gate")
    code = totp_now(secret)
    status, verified = api("/api/v1/admin/auth/mfa/verify", {"code": code}, token, expect=(200,))
    check(status == 200 and verified["verified"] and not verified["used_recovery_code"], "verifies via TOTP", failures)

    status, _past_gate = api(
        "/api/v1/admin/licenses",
        {"tenant_id": str(uuid.uuid4()), "plan_code": "does-not-matter"},
        token, expect=(404,),
    )
    check(
        status == 404, "the SAME request now fails downstream (no such plan), not on the step-up gate", failures,
    )

    step(7, "A recovery code verifies too, and is single-use")
    recovery_code = recovery_codes[0]
    status, verified = api("/api/v1/admin/auth/mfa/verify", {"recovery_code": recovery_code}, token, expect=(200,))
    check(status == 200 and verified["used_recovery_code"], "the recovery code verifies", failures)

    status, _ = api("/api/v1/admin/auth/mfa/verify", {"recovery_code": recovery_code}, token, expect=(401,))
    check(status == 401, "the same recovery code cannot be reused (401)", failures)

    _, status2 = api("/api/v1/admin/auth/mfa/status", token=token, method="GET")
    check(status2["recovery_codes_remaining"] == 9, "exactly one recovery code was consumed", failures)

    step(8, "With a fresh step-up (from the recovery-code verify), removing MFA now succeeds")
    status, _ = api("/api/v1/admin/auth/mfa/totp", token=token, method="DELETE", expect=(204,))
    check(status == 204, "DELETE succeeds once a recent step-up exists", failures)

    _, status3 = api("/api/v1/admin/auth/mfa/status", token=token, method="GET")
    check(status3 == {"enrolled": False, "recovery_codes_remaining": 0}, "fully unenrolled - totp and recovery codes both gone", failures)

    step(9, "Clean up")
    psql(f"DELETE FROM users WHERE id = '{admin_user_id}'")
    print("    test admin removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - TOTP enrollment, recovery codes, and the real step-up gate all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
