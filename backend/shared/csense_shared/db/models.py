"""SQLAlchemy 2.0 ORM models — Phase 1 scope: identity, tenancy, authorization, audit,
outbox. Field/table shape follows docs/05_BACKEND_SCHEMA.md exactly; each class docstring
cites the schema section it implements. Later phases add tables in their own migrations
without touching this file's existing tables (SCH rule: published/foundational tables are
additive, not rewritten in place).

Identifier note: docs/05_BACKEND_SCHEMA.md §1 prefers UUIDv7 and accepts UUIDv4 "where
libraries do not support v7." PostgreSQL 16 core has no native UUIDv7 generator, so this
schema uses `gen_random_uuid()` (UUIDv4, via the pgcrypto extension) for now. Switching to
UUIDv7 later is a values-only change (server_default), tracked in CHECKLIST.md.
"""
from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime.datetime]:
    return mapped_column(nullable=False, server_default=text("now()"))


def _updated_at() -> Mapped[datetime.datetime]:
    return mapped_column(
        nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


# --- Enums (SCH §5) --------------------------------------------------------------

user_status_enum = ENUM(
    "invited", "active", "locked", "disabled", "deleted", name="user_status", create_type=False
)
authenticator_type_enum = ENUM(
    "webauthn", "totp", "recovery_code", "oidc", name="authenticator_type", create_type=False
)
organization_type_enum = ENUM(
    "platform", "direct_customer", "reseller", "reseller_customer",
    name="organization_type", create_type=False,
)
organization_status_enum = ENUM(
    "provisioning", "active", "grace", "suspended", "closed",
    name="organization_status", create_type=False,
)
tenant_status_enum = ENUM(
    "provisioning", "active", "restricted", "suspended", "closed",
    name="tenant_status", create_type=False,
)
org_relationship_status_enum = ENUM(
    "active", "suspended", "ended", name="org_relationship_status", create_type=False
)
membership_status_enum = ENUM(
    "invited", "active", "suspended", "revoked", name="membership_status", create_type=False
)
site_scope_mode_enum = ENUM("all", "selected", "none", name="site_scope_mode", create_type=False)
role_type_enum = ENUM("system", "custom", name="role_type", create_type=False)
role_audience_enum = ENUM("customer", "platform", name="role_audience", create_type=False)
permission_effect_enum = ENUM("allow", "deny", name="permission_effect", create_type=False)
audit_outcome_enum = ENUM("success", "denied", "failed", name="audit_outcome", create_type=False)


# --- Identity (SCH §5.1 / §5.2) ---------------------------------------------------

class User(Base):
    """Platform-global identity. SCH §5.1."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_normalized: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    email_display: Mapped[str] = mapped_column(nullable=False)
    password_hash: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        user_status_enum, nullable=False, server_default="invited"
    )
    display_name: Mapped[str] = mapped_column(nullable=False)
    locale: Mapped[str] = mapped_column(nullable=False, server_default="en-US")
    timezone: Mapped[str] = mapped_column(nullable=False, server_default="UTC")
    email_verified_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    security_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()

    __table_args__ = (Index("ix_users_status_updated_at", "status", "updated_at"),)


class UserAuthenticator(Base):
    """SCH §5.2."""

    __tablename__ = "user_authenticators"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(authenticator_type_enum, nullable=False)
    credential_id: Mapped[str | None] = mapped_column(nullable=True)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(nullable=True)
    public_key: Mapped[bytes | None] = mapped_column(nullable=True)
    counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    label: Mapped[str | None] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("user_id", "type", "credential_id", name="uq_user_authenticator"),
    )


# --- Organizations & tenants (SCH §5.3 / §5.4 / §5.5) -----------------------------

class Organization(Base):
    """SCH §5.3."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_type: Mapped[str] = mapped_column(organization_type_enum, nullable=False)
    legal_name: Mapped[str] = mapped_column(nullable=False)
    display_name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        organization_status_enum, nullable=False, server_default="provisioning"
    )
    billing_reference: Mapped[str | None] = mapped_column(nullable=True)
    default_region: Mapped[str] = mapped_column(nullable=False, server_default="local")
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()


class Tenant(Base):
    """SCH §5.4. Tenant is the security/data boundary — every tenant-owned table below
    carries `tenant_id` and is subject to row-level security."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        tenant_status_enum, nullable=False, server_default="provisioning"
    )
    data_region: Mapped[str] = mapped_column(nullable=False, server_default="local")
    default_timezone: Mapped[str] = mapped_column(nullable=False, server_default="UTC")
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    security_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()


class OrganizationRelationship(Base):
    """SCH §5.5 — reseller/child relationship."""

    __tablename__ = "organization_relationships"

    id: Mapped[uuid.UUID] = _uuid_pk()
    parent_organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    child_organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(nullable=False, server_default="reseller_customer")
    status: Mapped[str] = mapped_column(
        org_relationship_status_enum, nullable=False, server_default="active"
    )
    effective_from: Mapped[datetime.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    effective_to: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    allocation_policy_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()

    __table_args__ = (
        Index(
            "uq_org_relationship_active",
            "parent_organization_id",
            "child_organization_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


# --- Roles, permissions, memberships (SCH §5.6 / §5.7 / §5.8) --------------------

class Role(Base):
    """SCH §5.7. Null tenant_id means a system role available to all tenants."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(nullable=False)
    role_type: Mapped[str] = mapped_column(role_type_enum, nullable=False, server_default="system")
    audience: Mapped[str] = mapped_column(role_audience_enum, nullable=False)
    description: Mapped[str | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()


class Permission(Base):
    """SCH §5.7. Permission codes follow `resource.action` (TRD §7.3)."""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(unique=True, nullable=False)
    resource: Mapped[str] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(nullable=False)
    risk_level: Mapped[str] = mapped_column(nullable=False, server_default="standard")
    description: Mapped[str | None] = mapped_column(nullable=True)


class RolePermission(Base):
    """SCH §5.7. Deny wins on conflict (TRD §7.3)."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )
    effect: Mapped[str] = mapped_column(permission_effect_enum, nullable=False, server_default="allow")


class Membership(Base):
    """SCH §5.6. Tenant-owned — subject to row-level security."""

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        membership_status_enum, nullable=False, server_default="invited"
    )
    site_scope_mode: Mapped[str] = mapped_column(
        site_scope_mode_enum, nullable=False, server_default="none"
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    invited_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    accepted_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()

    __table_args__ = (
        Index(
            "uq_membership_active_tenant_user",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_memberships_tenant_user", "tenant_id", "user_id"),
    )


class MembershipResourceScope(Base):
    """SCH §5.8. Tenant-owned — subject to row-level security."""

    __tablename__ = "membership_resource_scopes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(nullable=False)  # site | camera_group | camera
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    effect: Mapped[str] = mapped_column(permission_effect_enum, nullable=False, server_default="allow")
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (Index("ix_membership_scopes_tenant_membership", "tenant_id", "membership_id"),)


# --- Audit / outbox (SCH §11.1 / §11.2 / §11.3) -----------------------------------

class AuditEvent(Base):
    """SCH §11.1. Append-only; application roles get INSERT/SELECT only (enforced by
    migration GRANTs, not application logic alone)."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    actor_type: Mapped[str] = mapped_column(nullable=False)
    actor_id: Mapped[str | None] = mapped_column(nullable=True)
    represented_actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    support_grant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(nullable=False)
    target_type: Mapped[str | None] = mapped_column(nullable=True)
    target_id: Mapped[str | None] = mapped_column(nullable=True)
    outcome: Mapped[str] = mapped_column(audit_outcome_enum, nullable=False)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    before_patch: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_patch: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_hash_or_encrypted: Mapped[str | None] = mapped_column(nullable=True)
    device_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    previous_hash: Mapped[str | None] = mapped_column(nullable=True)
    event_hash: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_audit_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_events_action_occurred", "action", "occurred_at"),
    )


class OutboxEvent(Base):
    """SCH §11.2. Business mutation + event intent commit in the same transaction
    (architecture principle 8). A worker polls unpublished rows and publishes to Redis
    Streams / Pub/Sub, then stamps `published_at`."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    aggregate_type: Mapped[str] = mapped_column(nullable=False)
    aggregate_id: Mapped[str] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    published_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    last_error_redacted: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index(
            "ix_outbox_unpublished",
            "next_attempt_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class PlatformDeveloper(Base):
    """SCH §5.9. Platform-audience roles do not create tenant membership automatically."""

    __tablename__ = "platform_developers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(nullable=False, server_default="active")
    primary_team: Mapped[str | None] = mapped_column(nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    access_review_due_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()


class PlatformRoleAssignment(Base):
    """Grants a platform-audience role to a platform_developer. Not part of the literal
    schema doc (which specifies platform_developers but not how a role is attached to
    one) — see CLARIFICATIONS.md. Platform-global: no tenant_id column, so it is
    unreachable from any tenant-scoped repository by construction."""

    __tablename__ = "platform_role_assignments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    platform_developer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("platform_developers.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default="active")
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("platform_developer_id", "role_id", name="uq_platform_role_assignment"),
    )


class ProcessedEvent(Base):
    """SCH §11.3. Idempotency guard for consumers where domain uniqueness alone is
    insufficient (TRD §11.3: consumers must dedupe by event_id or idempotency key)."""

    __tablename__ = "processed_events"

    consumer_name: Mapped[str] = mapped_column(primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    result_hash: Mapped[str | None] = mapped_column(nullable=True)


# --- Object storage and AI model registry (SCH §8, §11.5) -------------------------

class StoredObject(Base):
    """SCH §11.5. Object keys stay opaque and tenant-prefixed; clients never build them."""

    __tablename__ = "stored_objects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    bucket: Mapped[str] = mapped_column(nullable=False)
    object_key: Mapped[str] = mapped_column(nullable=False)
    object_type: Mapped[str] = mapped_column(nullable=False)
    mime_type: Mapped[str | None] = mapped_column(nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(nullable=False)
    encryption_key_ref: Mapped[str | None] = mapped_column(nullable=True)
    retention_class: Mapped[str] = mapped_column(nullable=False, server_default="default")
    expires_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("bucket", "object_key", name="uq_stored_object_location"),
        Index("ix_stored_objects_sha256", "sha256"),
    )


class Model(Base):
    """SCH §8.1 — platform-global model family. No tenant_id: a model artifact is not
    tenant-owned data, so tenant repositories cannot reach this table at all."""

    __tablename__ = "models"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    task_code: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(nullable=True)
    owner_team: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, server_default="active")
    default_label_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = _updated_at()


class ModelVersion(Base):
    """SCH §8.2 — immutable, content-addressed model version.

    `artifact_sha256` is unique, and a database trigger (migration 0006) rejects updates
    to the identity/artifact columns, so a published version can never be repointed at
    different bytes. Only `state` transitions are permitted after creation.
    """

    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    model_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    version_label: Mapped[str] = mapped_column(nullable=False)
    artifact_object_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("stored_objects.id"), nullable=False
    )
    artifact_sha256: Mapped[str] = mapped_column(nullable=False)
    framework: Mapped[str] = mapped_column(nullable=False)
    runtime: Mapped[str] = mapped_column(nullable=False)
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    label_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    hardware_profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    license_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    access_classification: Mapped[str] = mapped_column(nullable=False, server_default="standard")
    state: Mapped[str] = mapped_column(
        ENUM(
            "uploaded", "validating", "validated", "staging", "production", "deprecated", "revoked",
            name="model_version_state", create_type=False,
        ),
        nullable=False,
        server_default="uploaded",
    )
    state_reason: Mapped[str | None] = mapped_column(nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("model_id", "version_label", name="uq_model_version_label"),
        UniqueConstraint("artifact_sha256", name="uq_model_version_artifact_sha256"),
        Index("ix_model_versions_model_state", "model_id", "state"),
    )


class ModelValidationRun(Base):
    """SCH §8.3 — records of the validation gates in TRD §15.2."""

    __tablename__ = "model_validation_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False
    )
    suite_version: Mapped[str] = mapped_column(nullable=False)
    environment: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default="pending")
    started_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    thresholds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_object_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("stored_objects.id"), nullable=True
    )
    failure_summary: Mapped[str | None] = mapped_column(nullable=True)
    runner_version: Mapped[str | None] = mapped_column(nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()
