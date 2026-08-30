"""Cross-service organization/tenant provisioning shared by Admin API and Tenant API.

TRD §7.2 keeps the two services' repository code from ever importing one another
directly - this package is the sanctioned shared home for logic both legitimately need,
the same role `csense_shared.audit.outbox` and `csense_shared.security.invitation_tickets`
already play.
"""
from __future__ import annotations

from csense_shared.tenancy.provisioning import (
    ProvisionedTenant,
    provision_organization_with_invited_owner,
    slugify,
)

__all__ = ["ProvisionedTenant", "provision_organization_with_invited_owner", "slugify"]
