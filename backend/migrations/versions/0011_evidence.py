"""Evidence records (docs/05_BACKEND_SCHEMA.md §9.6, TRD §17).

Evidence is an *authorised metadata record* pointing at media in object storage - the
bytes never live in PostgreSQL. Three properties the schema enforces rather than trusts:

1. **Digest before availability.** `sha256` is required, and SCH §19 says object metadata
   digest and size must match storage before evidence becomes available. A snapshot whose
   bytes do not match what was recorded is not evidence, it is an unverified file.

2. **Privacy variants are first-class.** `privacy_variant` plus `original_evidence_id`
   models the masked/unmasked pair explicitly. The masked variant is what a normal user
   sees; retrieving the original is a separate, permissioned act. Storing only one image
   and masking at render time would mean the unmasked bytes are one bug away from being
   served.

3. **Retention cannot silently delete evidence under legal hold.** `legal_hold` is checked
   by the lifecycle worker before any deletion (SCH §16).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-26
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, UUID as PG_UUID

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

EVIDENCE_TYPE = ["snapshot", "clip", "metadata"]
PRIVACY_VARIANT = ["original", "masked", "redacted"]


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (("evidence_type", EVIDENCE_TYPE), ("privacy_variant", PRIVACY_VARIANT)):
        ENUM(*values, name=name, create_type=True).create(bind, checkfirst=True)

    op.create_table(
        "evidence",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", PG_UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("camera_id", PG_UUID(as_uuid=True), sa.ForeignKey("cameras.id"), nullable=False),
        # Detections live in MongoDB, so this is a reference, not a foreign key.
        sa.Column("detection_id", sa.Text(), nullable=True),
        sa.Column("object_id", PG_UUID(as_uuid=True), sa.ForeignKey("stored_objects.id"), nullable=False),
        sa.Column("evidence_type", ENUM(*EVIDENCE_TYPE, name="evidence_type", create_type=False), nullable=False),
        sa.Column("capture_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "privacy_variant",
            ENUM(*PRIVACY_VARIANT, name="privacy_variant", create_type=False),
            nullable=False,
            server_default="masked",
        ),
        # Points from a masked variant back to the original it was derived from.
        sa.Column("original_evidence_id", PG_UUID(as_uuid=True), sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("retention_class", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("access_classification", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_sha256_format"),
        # A masked variant must say what it was derived from; an original must not.
        sa.CheckConstraint(
            "(privacy_variant = 'original' AND original_evidence_id IS NULL) "
            "OR (privacy_variant <> 'original')",
            name="ck_evidence_variant_lineage",
        ),
    )
    op.create_index("ix_evidence_tenant_incident", "evidence", ["tenant_id", "incident_id"])
    op.create_index("ix_evidence_tenant_capture", "evidence", ["tenant_id", "capture_time"])
    op.create_index(
        "ix_evidence_expiring", "evidence", ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL AND legal_hold = false"),
    )

    op.execute("ALTER TABLE evidence ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE evidence FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY evidence_tenant_isolation ON evidence
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

    for role_name in _app_roles():
        role = sa.sql.quoted_name(role_name, quote=True)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON evidence TO "{role}"')


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS evidence_tenant_isolation ON evidence")
    op.drop_table("evidence")
    op.execute("DROP TYPE IF EXISTS privacy_variant")
    op.execute("DROP TYPE IF EXISTS evidence_type")
