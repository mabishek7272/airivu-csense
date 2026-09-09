"""Imports the legacy CSense user directory into the new identity schema, force-resetting
every password rather than migrating or trusting any legacy hash (CLARIFICATIONS.md #30 -
DECIDED 2026-09-09, owner).

    python import_legacy_users.py --source /path/to/csense_users.db [--dry-run]

For each real legacy user (the one `@example.com` placeholder/test account is excluded -
logged as skipped, never silently dropped) this creates one new organization + tenant +
`invited` user + `invited` `tenant_owner` membership - the same shape self-registration
(`POST /api/v1/auth/register`) and `provision_organization_with_invited_owner`
(`backend/shared/csense_shared/tenancy/provisioning.py`) already produce for a brand-new
customer, except the owner here is `status='invited'`/`password_hash=NULL` instead of
active with a chosen password. `login()`'s own dummy-hash comparison already refuses a
null-password account correctly (see `identity.py`'s `create_invited_membership`
docstring), so nothing about authentication needed to change for this to be safe.

**No legacy password hash is ever read into memory.** The SQL below does not select the
`hashed_password` column at all - the only fact carried forward about it is that the
legacy schema's own `NOT NULL` constraint guarantees one existed, recorded in the audit
trail as a boolean, never as a value.

**Idempotent**: a legacy email that already exists as a `users.email_normalized` row is
skipped, not duplicated - safe to re-run against the same source file, or a refreshed one.

**Does not send any invitation email or create any invitation ticket.** That is the
separate, `--confirm-send`-gated `send_legacy_migration_invitations.py`, deliberately left
for the account owner to trigger.

**Verify-before-write**, like `import_legacy_models.py`: every legacy row is validated
against the source file before any Postgres write is attempted, so a malformed row fails
the run before anything is committed. Any error partway through the write loop rolls back
the whole batch (psycopg's own connection-context-manager behavior on an unhandled
exception) - a partial import is not a state this script can leave behind.

**What happens to fields with no home in the new schema:**
  - `phone` - the new `users` table has no phone column (checked directly against
    `csense_shared/db/models.py`; adding one is a schema change, out of scope for a data
    migration script). It is preserved only in this run's `audit_events.before_patch`, so
    the value is not lost entirely and could inform a future feature, without inventing
    a queryable column here.
  - legacy `created_at` - carried forward onto the new `users.created_at`, when parseable,
    so a migrated account doesn't look younger than it really is (its tenure with the
    product predates this migration). `organizations`/`tenants`/`memberships.created_at`
    are deliberately left at `now()` instead - those entities did not exist in the legacy
    system at all; backdating them would misrepresent when this tenant/org/membership was
    actually created.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

TEST_ACCOUNT_DOMAIN = "@example.com"

AUDIT_ACTION = "user.legacy_import"
AUDIT_ACTOR_ID = "import_legacy_users.py"
AUDIT_REASON = (
    "Legacy CSense user directory migration (CLARIFICATIONS.md #30). Force-reset, not "
    "re-hash: created invited with no password; a real invitation is sent separately by "
    "send_legacy_migration_invitations.py, on the account owner's own confirmation."
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


@dataclass(frozen=True)
class LegacyUser:
    legacy_id: int
    email: str
    first_name: str
    last_name: str
    phone: str | None
    is_active: bool | None
    created_at: dt.datetime | None
    provider: str | None
    onboarding_completed: bool | None

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def is_test_account(self) -> bool:
        return self.email.strip().lower().endswith(TEST_ACCOUNT_DOMAIN)


def _parse_legacy_datetime(raw: object) -> dt.datetime | None:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw
    try:
        return dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def read_legacy_users(source: Path) -> list[LegacyUser]:
    """Reads every row of the legacy `users` table - deliberately never selecting
    `hashed_password` (see module docstring)."""
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, email, first_name, last_name, phone, is_active, created_at, "
            "provider, onboarding_completed FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    return [
        LegacyUser(
            legacy_id=row["id"],
            email=(row["email"] or "").strip(),
            first_name=(row["first_name"] or "").strip(),
            last_name=(row["last_name"] or "").strip(),
            phone=row["phone"],
            is_active=bool(row["is_active"]) if row["is_active"] is not None else None,
            created_at=_parse_legacy_datetime(row["created_at"]),
            provider=row["provider"],
            onboarding_completed=(
                bool(row["onboarding_completed"]) if row["onboarding_completed"] is not None else None
            ),
        )
        for row in rows
    ]


def _slugify(name: str) -> str:
    # Mirrors app/api/auth.py::_slugify and csense_shared/tenancy/provisioning.py::slugify
    # exactly (random suffix for guaranteed uniqueness). Not imported from either - this is
    # a raw-psycopg operator script with no dependency on an async SQLAlchemy session or
    # tenant_api's `app` package - but organizations.slug is a real unique CITEXT column,
    # so the behavior has to match.
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _verify(legacy_users: list[LegacyUser]) -> tuple[list[LegacyUser], list[LegacyUser], list[str]]:
    """Splits legacy rows into (to_create, test_accounts, problems) without touching
    Postgres - the source-file-only half of verify-before-write."""
    problems: list[str] = []
    seen_emails: set[str] = set()
    to_create: list[LegacyUser] = []
    test_accounts: list[LegacyUser] = []

    for u in legacy_users:
        if not u.email or "@" not in u.email:
            problems.append(f"legacy id {u.legacy_id}: missing/invalid email")
            continue
        if not u.first_name or not u.last_name:
            problems.append(f"legacy id {u.legacy_id}: missing first/last name")
            continue
        key = u.email.lower()
        if key in seen_emails:
            problems.append(f"legacy id {u.legacy_id}: duplicate email within source file")
            continue
        seen_emails.add(key)

        if u.is_test_account:
            test_accounts.append(u)
        else:
            to_create.append(u)

    return to_create, test_accounts, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Path to the staged legacy csense_users.db SQLite file")
    parser.add_argument("--dry-run", action="store_true", help="Verify and print only; no Postgres writes")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print(f"Source file not found: {source}", file=sys.stderr)
        raise SystemExit(1)

    legacy_users = read_legacy_users(source)
    if not legacy_users:
        print("No rows found in the legacy users table.", file=sys.stderr)
        raise SystemExit(1)

    to_create, test_accounts, problems = _verify(legacy_users)

    if problems:
        print("Refusing to import; legacy row verification failed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"Verified {len(legacy_users)} legacy row(s): {len(to_create)} real, "
        f"{len(test_accounts)} test/placeholder.\n"
    )
    for u in test_accounts:
        print(f"  legacy id {u.legacy_id}: SKIP (test/placeholder account, domain={TEST_ACCOUNT_DOMAIN})")

    created = 0
    skipped_exists = 0

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        # memberships/audit_events are RLS-forced (SCH §15). This script connects as the
        # owner (a superuser), which bypasses RLS anyway; the flag is set for consistency
        # with how seed_dev_data.py already does this from the same kind of direct
        # connection.
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")

        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'"
        )
        row = cur.fetchone()
        if row is None:
            print("tenant_owner/customer role not found - has bootstrap_roles/seed run?", file=sys.stderr)
            raise SystemExit(1)
        owner_role_id = row[0]

        for u in to_create:
            cur.execute("SELECT id FROM users WHERE email_normalized = %s", (u.email.lower(),))
            existing = cur.fetchone()
            if existing is not None:
                print(f"  legacy id {u.legacy_id}: SKIP (already migrated -> user {existing[0]})")
                skipped_exists += 1
                continue

            if args.dry_run:
                print(f"  legacy id {u.legacy_id}: WOULD CREATE org + tenant + invited user + membership")
                created += 1
                continue

            org_name = u.display_name
            cur.execute(
                """
                INSERT INTO organizations (organization_type, legal_name, display_name, slug, status)
                VALUES ('direct_customer', %s, %s, %s, 'active')
                RETURNING id
                """,
                (org_name, org_name, _slugify(org_name)),
            )
            org_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id",
                (org_id,),
            )
            tenant_id = cur.fetchone()[0]

            if u.created_at is not None:
                cur.execute(
                    """
                    INSERT INTO users
                        (email_normalized, email_display, password_hash, status, display_name, created_at)
                    VALUES (%s, %s, NULL, 'invited', %s, %s)
                    RETURNING id
                    """,
                    (u.email.lower(), u.email, u.display_name, u.created_at),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (email_normalized, email_display, password_hash, status, display_name)
                    VALUES (%s, %s, NULL, 'invited', %s)
                    RETURNING id
                    """,
                    (u.email.lower(), u.email, u.display_name),
                )
            user_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO memberships
                    (tenant_id, user_id, role_id, status, site_scope_mode, invited_by, invited_at)
                VALUES (%s, %s, %s, 'invited', 'all', NULL, now())
                RETURNING id
                """,
                (tenant_id, user_id, owner_role_id),
            )
            membership_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO audit_events
                    (tenant_id, actor_type, actor_id, action, target_type, target_id, outcome,
                     reason, before_patch, after_patch)
                VALUES (%s, 'system', %s, %s, 'user', %s, 'success', %s, %s, %s)
                """,
                (
                    tenant_id,
                    AUDIT_ACTOR_ID,
                    AUDIT_ACTION,
                    str(user_id),
                    AUDIT_REASON,
                    psycopg.types.json.Json(
                        {
                            "legacy_id": u.legacy_id,
                            "legacy_provider": u.provider,
                            "legacy_password_hash_existed": True,
                            "legacy_password_hash_migrated": False,
                            "legacy_phone": u.phone,
                            "legacy_onboarding_completed": u.onboarding_completed,
                        }
                    ),
                    psycopg.types.json.Json(
                        {
                            "organization_id": str(org_id),
                            "tenant_id": str(tenant_id),
                            "user_id": str(user_id),
                            "membership_id": str(membership_id),
                            "status": "invited",
                        }
                    ),
                ),
            )

            print(
                f"  legacy id {u.legacy_id}: CREATED org={org_id} tenant={tenant_id} "
                f"user={user_id} membership={membership_id}"
            )
            created += 1

        if args.dry_run:
            print("\nDry run complete; nothing written.")
            return

        conn.commit()

    print(
        f"\nImport complete: {created} created, {skipped_exists} already migrated (skipped), "
        f"{len(test_accounts)} test/placeholder account(s) excluded."
    )


if __name__ == "__main__":
    main()
