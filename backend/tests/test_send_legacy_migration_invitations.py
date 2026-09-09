"""Real-database tests for `backend/migrations/send_legacy_migration_invitations.py`
(CLARIFICATIONS.md #30).

Builds its fixture data by actually running `import_legacy_users.py` against a synthetic
SQLite source first (never real customer data - `@legacy-invite-test.invalid` emails with
a per-test-run random UUID), the same way the real operator sequence works: import, then
invite. That also means these tests exercise the exact selection query
(`invited_by IS NULL` + a matching `user.legacy_import` audit row) against rows the import
script really created, not a hand-rolled stand-in for them.

**Two things this proves for real:**

  1. The safety rail: without `--confirm-send`, nothing is created and nothing is sent -
     proven by injecting fakes for both `create_invitation_ticket` and `build_registry`
     that raise `AssertionError` if called at all.

  2. A *fake* `--confirm-send` run: `create_invitation_ticket` and the email provider are
     both replaced with recording fakes (never real Redis, never real Resend), and the
     test asserts the real call shape - real UUIDs, the real recipient email, a `Message`
     with the real link embedded in its body, and a real `audit_events` row per send.

Needs a migrated database; skipped otherwise.
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

from csense_shared.notifications.providers import Channel, DeliveryOutcome, Message, SendResult

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def _load_module(name: str, filename: str):
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered before exec
    spec.loader.exec_module(module)
    return module


import_legacy_users = _load_module("csense_test_send_invites_import", "import_legacy_users.py")
send_invitations = _load_module("csense_test_send_invites_send", "send_legacy_migration_invitations.py")


def _dsn_kwargs() -> dict[str, str]:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return {
        "POSTGRES_USER": parts["user"],
        "POSTGRES_PASSWORD": parts["password"],
        "POSTGRES_HOST": parts["host"],
        "POSTGRES_PORT": parts.get("port", "5432"),
        "POSTGRES_DB": parts["dbname"],
    }


def _raw_dsn(env: dict[str, str]) -> str:
    return (
        f"host={env['POSTGRES_HOST']} port={env['POSTGRES_PORT']} "
        f"dbname={env['POSTGRES_DB']} user={env['POSTGRES_USER']} password={env['POSTGRES_PASSWORD']}"
    )


@pytest.fixture()
def pg_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    kwargs = _dsn_kwargs()
    for key, value in kwargs.items():
        monkeypatch.setenv(key, value)
    return kwargs


@pytest.fixture()
def migrated_users(pg_env, tmp_path, monkeypatch):
    """Runs the real import script against a 2-row synthetic legacy source, yielding
    (env, email_1, email_2). Tears down everything it created afterward."""
    suffix = uuid.uuid4().hex[:12]
    domain = "legacy-invite-test.invalid"
    email_1 = f"carol-{suffix}@{domain}"
    email_2 = f"dave-{suffix}@{domain}"

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
                (9201, email_1, "Carol", "Fixture", None, "$2b$12$" + "e" * 53, 1, "2023-02-01 00:00:00", "email", 0),
                (9202, email_2, "Dave", "Fixture", None, "$2b$12$" + "f" * 53, 1, "2023-03-01 00:00:00", "email", 0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr("sys.argv", ["import_legacy_users.py", "--source", str(db_path)])
    import_legacy_users.main()

    with psycopg.connect(_raw_dsn(pg_env)) as pg_conn, pg_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE email_normalized IN (%s, %s)",
            (email_1.lower(), email_2.lower()),
        )
        user_ids = [row[0] for row in cur.fetchall()]
    assert len(user_ids) == 2, "fixture setup didn't create both users - test can't proceed safely"

    try:
        yield pg_env, email_1, email_2, user_ids
    finally:
        with psycopg.connect(_raw_dsn(pg_env)) as pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT set_config('app.is_platform', 'true', false)")
            for email in (email_1, email_2):
                cur.execute("SELECT id FROM users WHERE email_normalized = %s", (email.lower(),))
                row = cur.fetchone()
                if row is None:
                    continue
                user_id = row[0]
                cur.execute("SELECT tenant_id FROM memberships WHERE user_id = %s", (user_id,))
                tenant_rows = cur.fetchall()
                cur.execute(
                    "DELETE FROM audit_events WHERE target_id = %s AND target_type IN ('user', 'membership')",
                    (str(user_id),),
                )
                for (tenant_id,) in tenant_rows:
                    cur.execute(
                        "SELECT id FROM memberships WHERE user_id = %s AND tenant_id = %s", (user_id, tenant_id)
                    )
                    membership_row = cur.fetchone()
                    if membership_row is not None:
                        cur.execute(
                            "DELETE FROM audit_events WHERE target_type = 'membership' AND target_id = %s",
                            (str(membership_row[0]),),
                        )
                cur.execute("DELETE FROM memberships WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                for (tenant_id,) in tenant_rows:
                    cur.execute("SELECT organization_id FROM tenants WHERE id = %s", (tenant_id,))
                    org_row = cur.fetchone()
                    cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                    if org_row is not None:
                        cur.execute("DELETE FROM organizations WHERE id = %s", (org_row[0],))
            pg_conn.commit()


class _FakeRedisClient:
    async def aclose(self) -> None:
        return None


class RecordingEmailProvider:
    def __init__(self, outcome: DeliveryOutcome = DeliveryOutcome.ACCEPTED):
        self.sent: list[Message] = []
        self._outcome = outcome

    @property
    def code(self) -> str:
        return "recording-fake"

    @property
    def channel(self) -> Channel:
        return Channel.EMAIL

    def validate_recipient(self, recipient: str) -> bool:
        return "@" in recipient

    async def send(self, message: Message) -> SendResult:
        self.sent.append(message)
        if self._outcome is DeliveryOutcome.ACCEPTED:
            return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id="fake-msg-1")
        return SendResult(self._outcome, failure_code="scripted", failure_summary="scripted")


class _ExplodingRegistry:
    def get(self, channel):
        raise AssertionError("build_registry()/registry.get() must not be called without --confirm-send")


async def _explode_create_ticket(*args, **kwargs):
    raise AssertionError("create_invitation_ticket must not be called without --confirm-send")


@pytest.mark.asyncio
async def test_without_confirm_send_nothing_happens(migrated_users, monkeypatch, capsys):
    pg_env, email_1, email_2, user_ids = migrated_users

    monkeypatch.setattr(send_invitations, "create_invitation_ticket", _explode_create_ticket)
    monkeypatch.setattr(send_invitations, "build_registry", lambda settings: _ExplodingRegistry())
    monkeypatch.setattr(send_invitations, "create_redis_client", lambda settings: _FakeRedisClient())

    # Scoped to exactly this fixture's own two users - never touches whatever else this
    # shared database happens to hold (a real gap found in code review: an unscoped call
    # here previously swept in every already-migrated real customer and wrote a false
    # 'sent' audit row against each one, every time this test ran).
    await send_invitations._run(confirm_send=False, user_ids=user_ids)

    out = capsys.readouterr().out
    assert email_1 in out
    assert email_2 in out
    assert "WOULD SEND" in out
    assert "no email was sent" in out
    # The real origin, not a placeholder domain - "the real link" requirement - but the
    # token portion is explicitly labeled as not issued.
    assert "issued-only-with---confirm-send" in out

    with psycopg.connect(_raw_dsn(pg_env)) as conn, conn.cursor() as cur:
        # Scoped to this fixture's own two memberships, not a bare global count - a
        # global count depends on this being the only thing that's ever touched this
        # table, which stopped being true the moment real data existed alongside tests.
        cur.execute(
            "SELECT count(*) FROM audit_events "
            "WHERE action = 'user.legacy_invitation_sent' "
            "AND target_id IN (SELECT id::text FROM memberships WHERE user_id = ANY(%s::uuid[]))",
            (user_ids,),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.asyncio
async def test_confirm_send_uses_real_call_shape_against_fakes(migrated_users, monkeypatch, capsys):
    pg_env, email_1, email_2, user_ids = migrated_users

    calls: list[dict] = []

    async def fake_create_invitation_ticket(redis_client, settings, *, membership_id, tenant_id, user_id, email):
        assert isinstance(redis_client, _FakeRedisClient)
        calls.append(
            {
                "membership_id": membership_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email": email,
            }
        )
        return f"fake-token-{len(calls)}"

    provider = RecordingEmailProvider()

    class _FakeRegistry:
        def get(self, channel):
            return provider

    monkeypatch.setattr(send_invitations, "create_invitation_ticket", fake_create_invitation_ticket)
    monkeypatch.setattr(send_invitations, "build_registry", lambda settings: _FakeRegistry())
    monkeypatch.setattr(send_invitations, "create_redis_client", lambda settings: _FakeRedisClient())

    # Scoped to exactly this fixture's own two users - see the same note in the sibling
    # test above. Without this, a shared database already holding real migrated
    # customers gets a real (if fake-provider) 'sent' audit row written against every
    # one of them, every time this test runs - which is exactly what happened before
    # this was found in code review.
    await send_invitations._run(confirm_send=True, user_ids=user_ids)

    out = capsys.readouterr().out
    assert "SENT ->" in out
    assert "Done: 2 sent, 0 failed" in out

    # Real shape: real UUIDs, real emails, one ticket call per candidate.
    assert len(calls) == 2
    called_emails = {c["email"] for c in calls}
    assert called_emails == {email_1, email_2}
    for c in calls:
        assert isinstance(c["membership_id"], uuid.UUID)
        assert isinstance(c["tenant_id"], uuid.UUID)
        assert isinstance(c["user_id"], uuid.UUID)

    # Real Message construction: the fake token actually lands in the email body.
    assert len(provider.sent) == 2
    for message in provider.sent:
        assert message.recipient in (email_1, email_2)
        assert "fake-token-" in message.body
        assert message.subject == "You've been invited to AIRIVU CSense"

    with psycopg.connect(_raw_dsn(pg_env)) as conn, conn.cursor() as cur:
        # Scoped the same way as the sibling test above, for the same reason.
        cur.execute(
            "SELECT outcome FROM audit_events "
            "WHERE action = 'user.legacy_invitation_sent' "
            "AND target_id IN (SELECT id::text FROM memberships WHERE user_id = ANY(%s::uuid[]))",
            (user_ids,),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert all(r[0] == "success" for r in rows)
