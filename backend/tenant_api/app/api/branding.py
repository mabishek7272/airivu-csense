"""The authenticated counterpart to `public_branding.py` - resolves the *caller's own*
organization's effective branding (own, or inherited from a reseller ancestor), never a
client-supplied slug or org id. This is what `BrandProvider` (frontend) calls right
after login to replace whatever the pre-login URL-derived branding showed, so the
session's real organization always wins over anything the address bar implied.

No permission check beyond a valid session: knowing your own organization's brand name
and colors is not privileged information relative to anything else a signed-in member
of that tenant can already see.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from csense_shared.config import Settings
from csense_shared.security.tenant_context import TenantContext
from csense_shared.storage.objects import public_branding_url

# Reuses public_branding.py's own response shape - the two endpoints answer the same
# question ("what should this UI look like") from different trust levels, not two
# different questions, so there is no reason for the payload shape to differ.
from app.api.public_branding import PublicBrandingOut

router = APIRouter(prefix="/api/v1/tenant/branding", tags=["tenant-branding"])


@router.get("/effective", response_model=PublicBrandingOut | None)
async def get_effective_branding(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> PublicBrandingOut | None:
    organization_id = (
        await db.execute(
            text("SELECT organization_id FROM tenants WHERE id = :tenant_id"),
            {"tenant_id": context.tenant_id},
        )
    ).scalar_one()

    row = (
        await db.execute(
            text(
                """
                SELECT slug, display_name, logo_object_id, favicon_object_id,
                       color_primary, color_accent, color_canvas
                FROM org_branding_resolve(:organization_id)
                """
            ),
            {"organization_id": organization_id},
        )
    ).mappings().first()

    # None (not 404) is the correct "unbranded" signal here: an authenticated request
    # for its own org's branding asking "do I have one" is a normal, expected outcome
    # for the default (non-white-labeled) majority of tenants, not an error condition -
    # unlike public_branding.py's 404, which means "this slug names nothing at all."
    if row is None:
        return None

    logo_key = favicon_key = None
    object_ids = [oid for oid in (row["logo_object_id"], row["favicon_object_id"]) if oid is not None]
    if object_ids:
        keys = (
            await db.execute(
                text("SELECT id, object_key FROM stored_objects WHERE id = ANY(:ids)"),
                {"ids": object_ids},
            )
        ).mappings().all()
        key_by_id = {k["id"]: k["object_key"] for k in keys}
        logo_key = key_by_id.get(row["logo_object_id"])
        favicon_key = key_by_id.get(row["favicon_object_id"])

    return PublicBrandingOut(
        slug=row["slug"],
        display_name=row["display_name"],
        logo_url=public_branding_url(settings, logo_key) if logo_key else None,
        favicon_url=public_branding_url(settings, favicon_key) if favicon_key else None,
        color_primary=row["color_primary"],
        color_accent=row["color_accent"],
        color_canvas=row["color_canvas"],
    )
