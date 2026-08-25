"""Grants the non-superuser API role the privileges it needs, and no more.

Pairs with backend/migrations/bootstrap_roles.py, which creates `csense_api` as
NOSUPERUSER/NOBYPASSRLS so row-level security actually applies to it (SCH §15). This
migration grants DML on the tables that role legitimately touches.

`audit_events` is deliberately INSERT + SELECT only: SCH §11.1 requires it be append-only
with "no update/delete permission for application roles". That is enforced here at the
database level, not merely by application convention — the API physically cannot rewrite
or erase an audit record.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25
"""
from __future__ import annotations

import os

from alembic import op
from sqlalchemy import sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = ("audit_events",)


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    for role_name in _app_roles():
        role = sql.quoted_name(role_name, quote=True)

        op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')

        # Read/write on ordinary domain tables.
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"'
        )
        op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"')

        # Then claw back write-beyond-append on the append-only tables.
        for table in APPEND_ONLY_TABLES:
            op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM "{role}"')

        # Tables created by later migrations inherit the same baseline automatically.
        op.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"'
        )
        op.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO "{role}"'
        )

        # Guard: if an application role could bypass RLS, every tenant-isolation policy
        # in this schema would be silently inert. Fail the migration rather than ship that.
        result = bind.execute(
            sql.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
            {"role": role_name},
        ).first()
        if result is None:
            raise RuntimeError(
                f"Application role {role_name!r} does not exist. "
                "Run backend/migrations/bootstrap_roles.py before applying migrations."
            )
        if result[0] or result[1]:
            raise RuntimeError(
                f"Application role {role_name!r} can bypass row-level security "
                f"(rolsuper={result[0]}, rolbypassrls={result[1]}). Refusing to continue."
            )


def downgrade() -> None:
    for role_name in _app_roles():
        role = sql.quoted_name(role_name, quote=True)
        op.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM "{role}"'
        )
        op.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM "{role}"'
        )
        op.execute(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM "{role}"')
        op.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"')
        op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{role}"')
