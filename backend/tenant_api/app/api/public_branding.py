"""Public, unauthenticated white-label branding lookup for the pre-login page - the
second genuinely public data endpoint in this codebase, after `auth.py`'s
register/login (which are also reachable with no prior session, but return an even
more sensitive payload than this one and are excluded from that "public data" framing
mainly by convention). Deliberately narrow: see `PublicBrandingOut` for exactly what
is and is not returned.

Trust boundary this endpoint sits on: the URL path is what selects a brand *before*
login. The instant a real session exists, `branding.py`'s `GET .../branding/effective`
(session-trusted, org id never taken from the client) replaces whatever this endpoint
returned - so a wrong or spoofed slug in the address bar can only ever affect what an
anonymous visitor sees on the login screen, never what an authenticated user sees after
signing in.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from csense_shared.db.postgres import bootstrap_session
from csense_shared.errors import NotFoundError
from csense_shared.storage.objects import public_branding_url

router = APIRouter(prefix="/api/v1/public/branding", tags=["public-branding"])


class PublicBrandingOut(BaseModel):
    slug: str
    display_name: str
    logo_url: str | None
    favicon_url: str | None
    color_primary: str | None
    color_accent: str | None
    color_canvas: str | None


@router.get("/{slug}", response_model=PublicBrandingOut)
async def get_public_branding(slug: str, request: Request) -> PublicBrandingOut:
    # Deliberately NOT validating slug format here beyond what the query itself does -
    # an invalid-shaped slug simply can never match a row (org_branding's own CHECK
    # constraint already guarantees every stored slug is well-formed), so a malformed
    # value reaches "not found" the same way an unbranded one does, with no separate
    # code path that would distinguish "badly formed" from "doesn't exist" to a caller.
    session_factory: async_sessionmaker = request.app.state.session_factory
    async with bootstrap_session(session_factory) as db:
        row = (
            await db.execute(
                text(
                    """
                    SELECT slug, display_name, logo_object_id, favicon_object_id,
                           color_primary, color_accent, color_canvas
                    FROM org_branding_resolve_by_slug(:slug)
                    """
                ),
                {"slug": slug},
            )
        ).mappings().first()

        if row is None:
            raise NotFoundError("No branding found for this path.")

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

    settings = request.app.state.settings
    return PublicBrandingOut(
        slug=row["slug"],
        display_name=row["display_name"],
        logo_url=public_branding_url(settings, logo_key) if logo_key else None,
        favicon_url=public_branding_url(settings, favicon_key) if favicon_key else None,
        color_primary=row["color_primary"],
        color_accent=row["color_accent"],
        color_canvas=row["color_canvas"],
    )
