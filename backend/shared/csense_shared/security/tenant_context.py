"""TenantContext: the only sanctioned source of tenant scoping for a request.

Per TRD §7.2: "The backend must not authorize from a client-supplied tenant_id.
Repository functions require an injected TenantContext." A TenantContext is only ever
constructed from a verified, signed access token's claims — never from a path/query/body
parameter. FastAPI dependencies build it once per request and everything downstream
(repositories, RLS session vars, audit events) takes it as an explicit argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    """Verified identity + tenant scope for one request. Immutable once built."""

    tenant_id: UUID
    user_id: UUID
    # None only for a support-grant-elevated platform-developer session (see
    # csense_shared.security.support_elevation) - there is no real memberships row for a
    # platform developer acting against a tenant they don't belong to.
    membership_id: UUID | None
    token_audience: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    site_scope_mode: str = "none"  # all | selected | none
    site_ids: frozenset[UUID] = field(default_factory=frozenset)
    support_grant_id: UUID | None = None
    correlation_id: str | None = None

    def has_permission(self, code: str) -> bool:
        # Deny wins: absence of an explicit allow is a deny. There is no implicit grant.
        return code in self.permissions


@dataclass(frozen=True)
class PlatformContext:
    """Verified platform-operator identity for Admin API cross-tenant operations.

    Distinct type from TenantContext on purpose: a function that accepts TenantContext
    cannot accidentally be called with platform-wide privileges, and vice versa.
    """

    developer_user_id: UUID
    token_audience: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    support_grant_id: UUID | None = None
    correlation_id: str | None = None

    def has_permission(self, code: str) -> bool:
        return code in self.permissions
