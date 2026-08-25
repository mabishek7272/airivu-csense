"""Permission-check helper implementing "deny wins" (TRD §7.3).

`resource.action` codes are attached to a TenantContext/PlatformContext at token-issuance
time from the membership's role + role_permissions (with any explicit deny at
role_permissions or membership_resource_scopes level removing the permission before the
token is even minted). At request time this is therefore a simple set-membership check —
there is no implicit grant, and a missing permission is always a deny.
"""
from __future__ import annotations

from csense_shared.errors import AuthorizationError
from csense_shared.security.tenant_context import PlatformContext, TenantContext


def require_permission(context: TenantContext | PlatformContext, code: str) -> None:
    if not context.has_permission(code):
        raise AuthorizationError(f"Missing required permission: {code}")
