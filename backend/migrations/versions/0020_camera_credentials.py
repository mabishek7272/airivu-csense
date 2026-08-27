"""Encrypted secret store, and the connection details a camera actually needs.

`cameras.endpoint_secret_id` has pointed at nothing since the schema was created - there
was no table to hold a secret and no way to encrypt one. Cameras also had no host, port or
stream path, so nothing could connect to one.

**Secrets live in their own table, not in a column on `cameras`.** Three reasons, and the
first is the one that matters:

  - A `SELECT *` on `cameras` is written constantly - in the API, in scripts, in a psql
    session during an incident. If the ciphertext lived there it would end up in logs,
    screenshots and support tickets. A separate table means reading a camera does not read
    its credential.
  - Grants can differ. Only the code paths that genuinely need to connect to a camera need
    `SELECT` here.
  - Other things will need secrets (ONVIF logins, webhook signing keys), and they should
    not each grow their own encryption.

**Ciphertext columns are text, not bytea.** They hold base64, which survives `pg_dump`,
CSV export and log redaction without binary-escaping surprises. The cost is ~33% size on
values that are a few hundred bytes.

**No plaintext column exists, at any point.** There is deliberately no "migrate later"
nullable plaintext field: a column that can hold a plaintext password eventually does.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "encrypted_secrets",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Nullable: platform-owned secrets (a gateway key) belong to no tenant. The
        # encryption binds to whichever it is, so the two can never be confused.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        # What this secret is for, e.g. 'camera.rtsp'. Authenticated as part of the
        # ciphertext, so a stored RTSP password cannot be opened as an API token.
        sa.Column("purpose", sa.Text(), nullable=False),
        # Which master key wrapped the data key. Rotation rewraps and updates this.
        sa.Column("kek_id", sa.Text(), nullable=False),
        sa.Column("wrapped_dek", sa.Text(), nullable=False),
        sa.Column("dek_nonce", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("ciphertext_nonce", sa.Text(), nullable=False),
        # Free-text label for an operator, e.g. "NVR admin". Never the secret itself.
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("length(purpose) BETWEEN 3 AND 64", name="ck_secret_purpose"),
        sa.CheckConstraint("length(kek_id) BETWEEN 1 AND 64", name="ck_secret_kek_id"),
    )

    # Finding every secret under a retired key is the first step of any rotation, and it
    # must not be a sequential scan over the whole table on a large deployment.
    op.create_index("ix_encrypted_secrets_kek", "encrypted_secrets", ["kek_id"])
    op.create_index(
        "ix_encrypted_secrets_tenant_purpose", "encrypted_secrets", ["tenant_id", "purpose"]
    )

    op.execute("ALTER TABLE encrypted_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE encrypted_secrets FORCE ROW LEVEL SECURITY")
    # A tenant sees only its own secrets. Platform-owned rows (tenant_id IS NULL) are
    # visible only to the platform role - deliberately not to any tenant, since those
    # protect shared infrastructure.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON encrypted_secrets
        USING (
            (tenant_id IS NOT NULL
             AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        WITH CHECK (
            (tenant_id IS NOT NULL
             AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            OR pg_has_role(current_user, 'csense_platform', 'MEMBER')
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON encrypted_secrets TO csense_api")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON encrypted_secrets TO csense_platform_api"
    )

    # --- Connection details on the camera ---------------------------------------------

    op.add_column("cameras", sa.Column("hostname", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("rtsp_port", sa.Integer(), nullable=True))
    # Split, not one URL: the credential must not be embeddable in the stored path, and a
    # main/sub split is what makes cheap inference and cheap live view possible at all
    # (the substream transcodes for a fraction of the mainstream's CPU).
    op.add_column("cameras", sa.Column("main_stream_path", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("sub_stream_path", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("username", sa.Text(), nullable=True))
    op.add_column(
        "cameras",
        sa.Column(
            "rtsp_transport", sa.Text(), nullable=False, server_default="tcp",
        ),
    )
    # What a probe found, so an operator is not guessing: codec, resolution, fps, GOP.
    op.add_column(
        "cameras",
        sa.Column(
            "stream_profile", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    op.add_column("cameras", sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cameras", sa.Column("last_error", sa.Text(), nullable=True))

    op.create_foreign_key(
        "cameras_endpoint_secret_id_fkey", "cameras", "encrypted_secrets",
        ["endpoint_secret_id"], ["id"], ondelete="SET NULL",
    )

    op.create_check_constraint(
        "ck_cameras_rtsp_port", "cameras",
        "rtsp_port IS NULL OR (rtsp_port BETWEEN 1 AND 65535)",
    )
    # UDP RTP across the public internet loses packets freely and produces smeared frames
    # that look like a camera fault. Restricting the values keeps a bad default out.
    op.create_check_constraint(
        "ck_cameras_rtsp_transport", "cameras", "rtsp_transport IN ('tcp', 'udp')",
    )
    # A stream path is a path, never a full URL - a URL could carry credentials, and those
    # must only ever live in encrypted_secrets.
    op.create_check_constraint(
        "ck_cameras_stream_paths_are_paths", "cameras",
        "(main_stream_path IS NULL OR main_stream_path LIKE '/%') "
        "AND (sub_stream_path IS NULL OR sub_stream_path LIKE '/%')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cameras_stream_paths_are_paths", "cameras", type_="check")
    op.drop_constraint("ck_cameras_rtsp_transport", "cameras", type_="check")
    op.drop_constraint("ck_cameras_rtsp_port", "cameras", type_="check")
    op.drop_constraint("cameras_endpoint_secret_id_fkey", "cameras", type_="foreignkey")
    for column in (
        "last_error", "last_probed_at", "stream_profile", "rtsp_transport", "username",
        "sub_stream_path", "main_stream_path", "rtsp_port", "hostname",
    ):
        op.drop_column("cameras", column)

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON encrypted_secrets")
    op.drop_index("ix_encrypted_secrets_tenant_purpose", table_name="encrypted_secrets")
    op.drop_index("ix_encrypted_secrets_kek", table_name="encrypted_secrets")
    op.drop_table("encrypted_secrets")
