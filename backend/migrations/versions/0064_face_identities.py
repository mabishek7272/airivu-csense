"""Face recognition: registered people (`face_identities`) and their encrypted face
templates (`face_templates`), plus `face.read` / `face.manage` permissions.

Biometric data, so two rules are built into the schema rather than left to callers:

**No consent, no row.** `consent_recorded_at` is NOT NULL. A person can only be registered
by recording that they agreed, and when.

**Templates are ciphertext, and there are no photos.** A face template is a 512-d embedding
that identifies a person. It is stored only through `encrypted_secrets` (the same AES-GCM
envelope camera passwords use, purpose `face.template`), referenced here by `secret_id`.
The uploaded photo is embedded and discarded; nothing image-shaped is persisted. Deleting a
template must delete its secret too (an FK cannot express that across tables, so the API
does it in the same transaction).

Permissions follow the tenant-role grant pattern of 0057: everyone in a tenant may read who
is registered, only tenant_owner and tenant_operator may register or remove people.

Revision ID: 0064
Revises: 0063
Create Date: 2026-09-24
"""
from __future__ import annotations

import os
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

_PERMISSIONS = [
    ("face.read", "face", "read", "elevated", "See which people are registered for face recognition"),
    ("face.manage", "face", "manage", "high", "Register and remove people (biometric face templates)"),
]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
        USING (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        WITH CHECK (
            tenant_id = current_setting('app.tenant_id', true)::uuid
            OR (
                coalesce(current_setting('app.is_platform', true)::boolean, false)
                AND pg_has_role(current_user, '{_platform_role()}', 'MEMBER')
            )
        )
        """
    )


def _grant(bind, role_name: str, codes: list[str]) -> None:
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :n AND audience = 'customer'"),
        {"n": role_name},
    ).scalar_one_or_none()
    if role_id is None:
        return
    for code in codes:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :c"), {"c": code}
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, effect) "
                "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
            ),
            {"r": role_id, "p": permission_id},
        )


def upgrade() -> None:
    op.create_table(
        "face_identities",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("list_type", sa.Text(), nullable=False, server_default="known"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("consent_recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consent_note", sa.Text(), nullable=False),
        sa.Column("created_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("list_type IN ('known', 'watchlist')", name="ck_face_identity_list_type"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_face_identity_status"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 120", name="ck_face_identity_name"),
    )
    op.create_index(
        "uq_face_identity_external_ref", "face_identities", ["tenant_id", "external_ref"],
        unique=True, postgresql_where=sa.text("external_ref IS NOT NULL"),
    )
    op.create_index("ix_face_identities_tenant", "face_identities", ["tenant_id", "status"])

    op.create_table(
        "face_templates",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "identity_id", PG_UUID(as_uuid=True),
            sa.ForeignKey("face_identities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("secret_id", PG_UUID(as_uuid=True), sa.ForeignKey("encrypted_secrets.id"), nullable=False),
        sa.Column("model_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("detector_confidence", sa.Numeric(6, 5), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_face_templates_identity", "face_templates", ["tenant_id", "identity_id"])

    for table in ("face_identities", "face_templates"):
        _enable_rls(table)
        for role_name in _app_roles():
            role = sa.sql.quoted_name(role_name, quote=True)
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"')

    bind = op.get_bind()
    for code, resource, action, risk, description in _PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
                "VALUES (:id, :code, :resource, :action, :risk, :desc) ON CONFLICT (code) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "code": code, "resource": resource, "action": action,
             "risk": risk, "desc": description},
        )
    for role in ("tenant_owner", "tenant_operator"):
        _grant(bind, role, ["face.read", "face.manage"])
    for role in ("tenant_member", "tenant_viewer"):
        _grant(bind, role, ["face.read"])


def downgrade() -> None:
    bind = op.get_bind()
    codes = [p[0] for p in _PERMISSIONS]
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = ANY(:codes))"
        ),
        {"codes": codes},
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": codes})
    op.drop_table("face_templates")
    op.drop_table("face_identities")
