"""Creates the non-superuser application login roles before migrations run.

Why this exists (SCH §15: "Tenant DB role cannot bypass RLS", "Admin API uses a separate
restricted platform role"):

The `POSTGRES_USER` created by the postgres image is a SUPERUSER, and PostgreSQL
superusers bypass row-level security entirely — `FORCE ROW LEVEL SECURITY` does not
apply to them. If the APIs connected as that user, every RLS policy in this schema would
be silently inert and cross-tenant isolation would be a no-op while still appearing
configured. This was caught by the tenant-isolation test during Phase 1.

Three database identities:

  csense_app (POSTGRES_USER)  superuser/owner. Migrations and DDL only.
  csense_api                  Tenant API. NOSUPERUSER NOBYPASSRLS, NOT a member of
                              csense_platform — so it cannot reach another tenant's rows
                              even if application code is compromised.
  csense_platform_api         Admin API. Same restrictions, but a member of
                              csense_platform, which the RLS policies require for the
                              cross-tenant bypass (see migration 0005).

  csense_platform             NOLOGIN group role. Membership in it is what the policies
                              actually test.

Idempotent: safe to run on every startup, and works both locally and in CI (where the
postgres service container never runs docker-entrypoint-initdb.d scripts).
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql

PLATFORM_GROUP_ROLE = "csense_platform"


def _owner_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _ensure_login_role(cur, name: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    exists = cur.fetchone() is not None
    verb = "ALTER" if exists else "CREATE"
    cur.execute(
        sql.SQL(
            verb + " ROLE {} WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}"
        ).format(sql.Identifier(name), sql.Literal(password))
    )
    print(f"{'Updated' if exists else 'Created'} login role {name!r} (NOSUPERUSER, NOBYPASSRLS).")


def _assert_cannot_bypass_rls(cur, name: str) -> None:
    cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (name,))
    is_super, bypasses_rls = cur.fetchone()
    if is_super or bypasses_rls:
        print(
            f"Refusing to continue: role {name!r} can bypass row-level security "
            f"(rolsuper={is_super}, rolbypassrls={bypasses_rls}).",
            file=sys.stderr,
        )
        raise SystemExit(1)


# Databases the WhatsApp gateway owns. Kept separate from the platform database on
# purpose: the gateway stores WhatsApp session credentials, and that material should not
# share a schema - or a backup - with tenant business data.
WHATSAPP_DATABASES = ("whatsapp_auth", "whatsapp_users")


def _ensure_database(cur, name: str) -> None:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    if cur.fetchone() is None:
        # CREATE DATABASE cannot run inside a transaction; this connection is autocommit.
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        print(f"Created database {name!r} for the WhatsApp gateway.")


def main() -> None:
    api_user = os.environ.get("POSTGRES_API_USER", "csense_api")
    api_password = os.environ.get("POSTGRES_API_PASSWORD")
    platform_user = os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api")
    platform_password = os.environ.get("POSTGRES_PLATFORM_API_PASSWORD")

    missing = [
        name
        for name, value in (
            ("POSTGRES_API_PASSWORD", api_password),
            ("POSTGRES_PLATFORM_API_PASSWORD", platform_password),
        )
        if not value
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)

    with psycopg.connect(_owner_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (PLATFORM_GROUP_ROLE,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} WITH NOLOGIN").format(sql.Identifier(PLATFORM_GROUP_ROLE)))
            print(f"Created group role {PLATFORM_GROUP_ROLE!r} (NOLOGIN).")

        _ensure_login_role(cur, api_user, api_password)
        _ensure_login_role(cur, platform_user, platform_password)

        # Only the Admin API's role joins the platform group.
        cur.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(PLATFORM_GROUP_ROLE), sql.Identifier(platform_user)
            )
        )
        # Defensive: make sure the tenant role never drifts into the platform group.
        cur.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(PLATFORM_GROUP_ROLE), sql.Identifier(api_user)
            )
        )

        for database in WHATSAPP_DATABASES:
            _ensure_database(cur, database)

        for role in (api_user, platform_user):
            _assert_cannot_bypass_rls(cur, role)

        cur.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'MEMBER')",
            (api_user, PLATFORM_GROUP_ROLE, platform_user, PLATFORM_GROUP_ROLE),
        )
        tenant_in_group, platform_in_group = cur.fetchone()
        if tenant_in_group:
            print(
                f"Refusing to continue: tenant role {api_user!r} is a member of "
                f"{PLATFORM_GROUP_ROLE!r} and could bypass tenant isolation.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not platform_in_group:
            print(
                f"Refusing to continue: platform role {platform_user!r} is not a member of "
                f"{PLATFORM_GROUP_ROLE!r}; the Admin API would be unable to operate.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        print("Database roles verified: tenant role isolated, platform role authorized.")


if __name__ == "__main__":
    main()
