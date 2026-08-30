"""Licensing and quota enforcement shared by Tenant API and Admin API - see
`csense_shared.licensing.quota` for the concurrent reservation pattern
(docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §9) and `csense_shared.licensing.lifecycle`
for grace/expiry and restriction."""
from __future__ import annotations

from csense_shared.licensing.lifecycle import (
    RESTRICTED_STATUSES,
    current_license,
    require_license_not_restricted,
    sync_license_status,
)
from csense_shared.licensing.quota import QuotaExceededError, reserve_quota

__all__ = [
    "QuotaExceededError",
    "reserve_quota",
    "RESTRICTED_STATUSES",
    "current_license",
    "require_license_not_restricted",
    "sync_license_status",
]
