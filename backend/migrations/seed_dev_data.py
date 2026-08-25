"""Creates local-only smoke-test accounts: one tenant owner and one platform admin.

Run after `alembic upgrade head`:
    docker compose -f infra/docker-compose.yml run --rm migrate python seed_dev_data.py

Passwords are randomly generated and printed once — never stored in this repo. This is a
development convenience only; production account creation goes through the real
registration/invitation flows (Phase 1 `/auth/register`, Phase 2 invitations), never a
seed script.
"""
from __future__ import annotations

import os
import secrets

import psycopg

from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def main() -> None:
    settings = get_settings()
    tenant_password = secrets.token_urlsafe(18)
    platform_password = secrets.token_urlsafe(18)

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        # memberships/membership_resource_scopes/audit_events are RLS-forced (SCH §15).
        # This script connects as the owner (a superuser), which bypasses RLS anyway; the
        # flag is set for consistency with how the services scope their own sessions.
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")

        # --- demo tenant owner (org "Acme Demo") ---
        cur.execute("SELECT id FROM organizations WHERE slug = 'acme-demo'")
        row = cur.fetchone()
        if row:
            print("Demo tenant already exists (organizations.slug='acme-demo'); skipping.")
        else:
            cur.execute(
                """
                INSERT INTO organizations (organization_type, legal_name, display_name, slug, status)
                VALUES ('direct_customer', 'Acme Demo Ltd', 'Acme Demo', 'acme-demo', 'active')
                RETURNING id
                """
            )
            org_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id",
                (org_id,),
            )
            tenant_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO users (email_normalized, email_display, password_hash, status, display_name)
                VALUES (%s, %s, %s, 'active', 'Demo Owner') RETURNING id
                """,
                ("owner@acme-demo.dev", "owner@acme-demo.dev", hash_password(tenant_password, settings)),
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'"
            )
            role_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at)
                VALUES (%s, %s, %s, 'active', now())
                """,
                (tenant_id, user_id, role_id),
            )
            print(f"Created tenant owner: owner@acme-demo.dev / {tenant_password}")

        # --- demo platform admin ---
        cur.execute("SELECT id FROM users WHERE email_normalized = 'admin@platform.dev'")
        row = cur.fetchone()
        if row:
            print("Demo platform admin already exists; skipping.")
        else:
            cur.execute(
                """
                INSERT INTO users (email_normalized, email_display, password_hash, status, display_name)
                VALUES (%s, %s, %s, 'active', 'Demo Platform Admin') RETURNING id
                """,
                ("admin@platform.dev", "admin@platform.dev", hash_password(platform_password, settings)),
            )
            admin_user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
                (admin_user_id,),
            )
            developer_id = cur.fetchone()[0]
            cur.execute(
                "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'"
            )
            role_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) VALUES (%s, %s, 'active')",
                (developer_id, role_id),
            )
            print(f"Created platform admin: admin@platform.dev / {platform_password}")

        conn.commit()


if __name__ == "__main__":
    main()
