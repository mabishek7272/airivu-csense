"""Real-database tests for `backend/migrations/import_legacy_users.py`
(CLARIFICATIONS.md #30).

Uses a small SYNTHETIC SQLite fixture built in-process (never real customer data) shaped
like the real legacy schema: two ordinary rows plus one `@example.com` placeholder row,
matching the one real test/placeholder account the real legacy database is known to carry.

Every row this test creates uses a `@legacy-import-test.invalid` domain with a
per-test-run random UUID in the local part, so it can never collide with a real
customer's email and is trivially identifiable for teardown - the fixture always deletes
everything it created, in a `finally`, regardless of pass/fail.

Needs a migrated database; skipped otherwise (same convention as
`test_notification_worker.py` and friends).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import sys
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _load_module():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "import_legacy_users.py"
    spec = importlib.util.spec_from_file_location("csense_test_import_legacy_users", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered before exec
    spec.loader.exec_module(module)
    return module


import_legacy_users = _load_module()


def _dsn_kwargs() -> dict[str, str]:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return {
        "POSTGRES_USER": parts["user"],
        "POSTGRES_PASSWORD": parts["password"],
        "POSTGRES_HOST": parts["host"],
        "POSTGRES_PORT": parts.get("port", "5432"),
        "POSTGRES_DB": parts["dbname"],
    }


@pytest.fixture()
def pg_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    kwargs = _dsn_kwargs()
    for key, value in kwargs.items():
        monkeypatch.setenv(key, value)
    return kwargs


@pytest.fixture()
def legacy_sqlite(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str, str, str]:
    """A synthetic legacy `users.db` with 2 real rows + 1 `@example.com` placeholder -
    same shape as the real 61-row source (60 real + 1 excluded), just three rows."""
    suffix = uuid.uuid4().hex[:12]
    domain = "legacy-import-test.invalid"
    real_email_1 = f"alice-{suffix}@{domain}"
    real_email_2 = f"bob-{suffix}@{domain}"
    test_email = f"placeholder-{suffix}@example.com"

    db_path = tmp_path / "csense_users.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, first_name VARCHAR NOT NULL,
                last_name VARCHAR NOT NULL, phone VARCHAR, hashed_password VARCHAR NOT NULL,
                is_active BOOLEAN, created_at DATETIME, provider VARCHAR,
                onboarding_completed BOOLEAN DEFAULT 0
            )
            """
        )
        conn.executemany(
            "INSERT INTO users (id, email, first_name, last_name, phone, hashed_password, "
            "is_active, created_at, provider, onboarding_completed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    9001, real_email_1, "Alice", "Fixture", "+15550000001",
                    "$2b$12$" + "a" * 53, 1, "2023-05-01 12:00:00", "email", 0,
                ),
                (
                    9002, real_email_2, "Bob", "Fixture", None,
                    "sso-google", 1, "2023-06-15 08:30:00", "google", 1,
                ),
                (
                    9003, test_email, "Test", "User", None,
                    "$2b$12$" + "b" * 53, 1, "2020-01-01 00:00:00", "email", 0,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return db_path, real_email_1, real_email_2, test_email


def _cleanup(dsn_env: dict[str, str], *emails: str) -> None:
    dsn = (
        f"host={dsn_env['POSTGRES_HOST']} port={dsn_env['POSTGRES_PORT']} "
        f"dbname={dsn_env['POSTGRES_DB']} user={dsn_env['POSTGRES_USER']} password={dsn_env['POSTGRES_PASSWORD']}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        for email in emails:
            cur.execute("SELECT id FROM users WHERE email_normalized = %s", (email.lower(),))
            row = cur.fetchone()
            if row is None:
                continue
            user_id = row[0]
            cur.execute("SELECT tenant_id FROM memberships WHERE user_id = %s", (user_id,))
            tenant_rows = cur.fetchall()
            cur.execute("DELETE FROM audit_events WHERE target_type = 'user' AND target_id = %s", (str(user_id),))
            cur.execute("DELETE FROM memberships WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            for (tenant_id,) in tenant_rows:
                cur.execute("SELECT organization_id FROM tenants WHERE id = %s", (tenant_id,))
                org_row = cur.fetchone()
                cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                if org_row is not None:
                    cur.execute("DELETE FROM organizations WHERE id = %s", (org_row[0],))
        conn.commit()


def _query_user(dsn_env: dict[str, str], email: str):
    dsn = (
        f"host={dsn_env['POSTGRES_HOST']} port={dsn_env['POSTGRES_PORT']} "
        f"dbname={dsn_env['POSTGRES_DB']} user={dsn_env['POSTGRES_USER']} password={dsn_env['POSTGRES_PASSWORD']}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, password_hash, display_name FROM users WHERE email_normalized = %s",
            (email.lower(),),
        )
        return cur.fetchone()


def test_dry_run_makes_no_changes(pg_env, legacy_sqlite, monkeypatch, capsys):
    db_path, real_email_1, real_email_2, test_email = legacy_sqlite
    monkeypatch.setattr(
        "sys.argv", ["import_legacy_users.py", "--source", str(db_path), "--dry-run"]
    )
    try:
        import_legacy_users.main()
        out = capsys.readouterr().out
        assert "2 real" in out
        assert "1 test/placeholder" in out
        assert "Dry run complete" in out
        assert _query_user(pg_env, real_email_1) is None
        assert _query_user(pg_env, real_email_2) is None
        assert _query_user(pg_env, test_email) is None
    finally:
        _cleanup(pg_env, real_email_1, real_email_2, test_email)


def test_import_creates_invited_users_and_excludes_test_account(pg_env, legacy_sqlite, monkeypatch, capsys):
    db_path, real_email_1, real_email_2, test_email = legacy_sqlite
    monkeypatch.setattr("sys.argv", ["import_legacy_users.py", "--source", str(db_path)])
    try:
        import_legacy_users.main()
        out = capsys.readouterr().out
        assert "2 created" in out
        assert "1 test/placeholder account(s) excluded" in out

        row1 = _query_user(pg_env, real_email_1)
        row2 = _query_user(pg_env, real_email_2)
        assert row1 is not None and row2 is not None

        # The exact property this migration exists to guarantee: no legacy hash
        # survived, and the account cannot be used to log in until it accepts a real
        # invitation.
        for row in (row1, row2):
            user_id, status, password_hash, display_name = row
            assert status == "invited"
            assert password_hash is None

        assert row1[3] == "Alice Fixture"
        assert row2[3] == "Bob Fixture"

        # The test/placeholder account must never appear as a real users row.
        assert _query_user(pg_env, test_email) is None

        dsn = (
            f"host={pg_env['POSTGRES_HOST']} port={pg_env['POSTGRES_PORT']} "
            f"dbname={pg_env['POSTGRES_DB']} user={pg_env['POSTGRES_USER']} password={pg_env['POSTGRES_PASSWORD']}"
        )
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            for user_id in (row1[0], row2[0]):
                cur.execute(
                    "SELECT action, actor_type, actor_id, outcome, before_patch, after_patch "
                    "FROM audit_events WHERE target_type = 'user' AND target_id = %s",
                    (str(user_id),),
                )
                audit_rows = cur.fetchall()
                assert len(audit_rows) == 1
                action, actor_type, actor_id, outcome, before_patch, after_patch = audit_rows[0]
                assert action == "user.legacy_import"
                assert actor_type == "system"
                assert actor_id == "import_legacy_users.py"
                assert outcome == "success"
                assert before_patch["legacy_password_hash_existed"] is True
                assert before_patch["legacy_password_hash_migrated"] is False
                assert after_patch["status"] == "invited"

                cur.execute(
                    "SELECT status, site_scope_mode, invited_by, role_id FROM memberships WHERE user_id = %s",
                    (user_id,),
                )
                membership_row = cur.fetchone()
                assert membership_row is not None
                m_status, m_scope, invited_by, role_id = membership_row
                assert m_status == "invited"
                assert m_scope == "all"
                assert invited_by is None

                cur.execute(
                    "SELECT name, audience FROM roles WHERE id = %s", (role_id,)
                )
                role_name, audience = cur.fetchone()
                assert role_name == "tenant_owner"
                assert audience == "customer"

        # --- idempotency: re-running creates nothing new -----------------------------
        import_legacy_users.main()
        out2 = capsys.readouterr().out
        assert "0 created" in out2
        assert "2 already migrated (skipped)" in out2

        row1_again = _query_user(pg_env, real_email_1)
        row2_again = _query_user(pg_env, real_email_2)
        assert row1_again[0] == row1[0]
        assert row2_again[0] == row2[0]
    finally:
        _cleanup(pg_env, real_email_1, real_email_2, test_email)


def test_bad_row_fails_before_any_write(pg_env, tmp_path, monkeypatch, capsys):
    """A row missing a required field must fail verification before Postgres is ever
    touched - and must not create a half-imported organization/tenant for the other,
    valid row in the same file."""
    suffix = uuid.uuid4().hex[:12]
    domain = "legacy-import-test.invalid"
    good_email = f"good-{suffix}@{domain}"

    db_path = tmp_path / "csense_users.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, first_name VARCHAR NOT NULL,
                last_name VARCHAR NOT NULL, phone VARCHAR, hashed_password VARCHAR NOT NULL,
                is_active BOOLEAN, created_at DATETIME, provider VARCHAR,
                onboarding_completed BOOLEAN DEFAULT 0
            )
            """
        )
        conn.executemany(
            "INSERT INTO users (id, email, first_name, last_name, phone, hashed_password, "
            "is_active, created_at, provider, onboarding_completed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (9101, good_email, "Good", "Fixture", None, "$2b$12$" + "c" * 53, 1, "2023-01-01 00:00:00", "email", 0),
                (9102, f"bad-{suffix}@{domain}", "", "MissingFirstName", None, "$2b$12$" + "d" * 53, 1, None, "email", 0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr("sys.argv", ["import_legacy_users.py", "--source", str(db_path)])
    try:
        with pytest.raises(SystemExit):
            import_legacy_users.main()
        err = capsys.readouterr().err
        assert "verification failed" in err
        assert _query_user(pg_env, good_email) is None
    finally:
        _cleanup(pg_env, good_email)
