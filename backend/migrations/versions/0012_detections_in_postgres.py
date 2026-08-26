"""Moves detections from MongoDB to PostgreSQL.

A deliberate departure from docs/05_BACKEND_SCHEMA.md §2, which assigns high-volume
detections to MongoDB. Recorded here rather than in a commit message because anyone
reading the schema doc will otherwise expect a second datastore that no longer exists.

Why the change is the right one for this system:

1. **Tenant isolation becomes database-enforced.** In MongoDB it was application-level
   only - a wrapper that folded `tenant_id` into every filter. That is one forgotten call
   away from a cross-tenant read. Here the same row-level security that protects every
   other tenant-owned table applies, gated on `csense_platform` group membership, so a
   compromised Tenant API still cannot read across tenants. This is a security
   improvement, not a lateral move.

2. **Detections and incidents commit together.** They previously lived in different
   stores, so a detection could be written and its incident lost, or the reverse, with no
   transaction spanning them. `incident_detection_links.detection_id` was a bare string
   with no referential integrity; it is now a real foreign key.

3. **One datastore to operate.** Backup, restore, PITR, monitoring, upgrades and patching
   halve. TRD §2 principle 7 is "simple before distributed", and running MongoDB for a
   single collection was not earning its operational cost.

The tradeoff is write throughput at extreme scale, which is what the original choice was
guarding against. The design inputs are 200 detections/sec sustained and 1,000/sec burst
(TRD §25) - roughly 17M rows/day sustained. PostgreSQL handles that on one node, but not
indefinitely without partitioning.

**Partitioning is deliberately NOT done now.** SCH §17 says to partition "once measured
volume justifies it", and partitioning would force the idempotency constraint to include
the partition key - weakening `(tenant_id, source_event_id)` to include `capture_time`,
which is exactly the guarantee that stops an edge device's spool replay creating
duplicates. Trigger to revisit: sustained >10M rows/month or degrading p95 on the camera
history query. At that point convert to monthly RANGE partitions on `capture_time`.

Retention: PostgreSQL has no TTL index, so the `expires_at` column is swept by
`delete_expired_detections()` rather than by the storage engine. A null `expires_at`
means "never expire", which is how legal hold keeps a row out of the sweeper's reach.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-26
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _platform_role() -> str:
    return os.environ.get("POSTGRES_PLATFORM_GROUP", "csense_platform")


def _app_roles() -> list[str]:
    return [
        os.environ.get("POSTGRES_API_USER", "csense_api"),
        os.environ.get("POSTGRES_PLATFORM_API_USER", "csense_platform_api"),
    ]


def upgrade() -> None:
    op.create_table(
        "detections",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", PG_UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("camera_id", PG_UUID(as_uuid=True), sa.ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False),
        sa.Column("edge_device_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("pipeline_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("model_version_id", PG_UUID(as_uuid=True), sa.ForeignKey("model_versions.id"), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        # The producing device's own id for this observation - the idempotency key.
        sa.Column("source_event_id", sa.Text(), nullable=False),
        # Five distinct timestamps (TRD-DATA-005). They diverge when a device has a skewed
        # clock or replays a backlog, which is when an investigator most needs them apart.
        sa.Column("capture_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("edge_receive_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cloud_receive_time", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("confidence", sa.Numeric(6, 5), nullable=False),
        # Bounded payload: the detected objects for this frame. JSONB keeps the flexible
        # shape MongoDB was chosen for, without the second datastore.
        sa.Column("objects", JSONB(), nullable=False, server_default="[]"),
        sa.Column("roi_id", sa.Text(), nullable=True),
        sa.Column("rule_results", JSONB(), nullable=False, server_default="[]"),
        sa.Column("evidence_refs", JSONB(), nullable=False, server_default="[]"),
        sa.Column("correlation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        # Null means never expire - how legal hold stays out of the sweeper's reach.
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        # The guarantee that makes edge retries and spool replay safe.
        sa.UniqueConstraint("tenant_id", "source_event_id", name="uq_detection_tenant_source_event"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_detection_confidence_range"),
    )

    # Every high-cardinality index starts with tenant_id (SCH §17), so tenant-scoped
    # queries stay selective and none can accidentally scan across tenants.
    op.create_index(
        "ix_detections_tenant_camera_capture",
        "detections",
        ["tenant_id", "camera_id", sa.text("capture_time DESC")],
    )
    op.create_index(
        "ix_detections_tenant_type_capture",
        "detections",
        ["tenant_id", "event_type", sa.text("capture_time DESC")],
    )
    op.create_index(
        "ix_detections_tenant_correlation",
        "detections",
        ["tenant_id", "correlation_id"],
        postgresql_where=sa.text("correlation_id IS NOT NULL"),
    )
    # BRIN rather than btree for the retention sweep: detections are inserted in roughly
    # capture_time order, so a BRIN index is a few kilobytes where a btree over tens of
    # millions of rows would be gigabytes. It only needs to find old rows in bulk.
    op.create_index(
        "brin_detections_expires_at",
        "detections",
        ["expires_at"],
        postgresql_using="brin",
        postgresql_with={"pages_per_range": 128},
    )
    # No GIN on `objects`: SCH §17 warns against speculative JSONB indexing, and nothing
    # currently queries inside the payload. Add one when a real query needs it.

    op.execute("ALTER TABLE detections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE detections FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY detections_tenant_isolation ON detections
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
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON detections TO "{role}"')

    # incident_detection_links.detection_id was a bare text reference to a MongoDB
    # document. Now that detections are in the same database it becomes a real foreign
    # key, so an incident can no longer point at a detection that does not exist.
    op.execute("DELETE FROM incident_detection_links")  # test-only rows referencing Mongo ids
    op.alter_column(
        "incident_detection_links",
        "detection_id",
        type_=PG_UUID(as_uuid=True),
        postgresql_using="detection_id::uuid",
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_incident_detection_links_detection",
        "incident_detection_links",
        "detections",
        ["detection_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_incident_detection_links_detection", "incident_detection_links", type_="foreignkey"
    )
    op.alter_column(
        "incident_detection_links",
        "detection_id",
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.execute("DROP POLICY IF EXISTS detections_tenant_isolation ON detections")
    op.drop_table("detections")
