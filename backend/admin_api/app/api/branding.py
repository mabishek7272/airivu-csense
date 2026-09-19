"""Platform management of per-organization white-label branding
(`organization.branding.manage`). Admin-managed only in v1 - no self-service branding
UI exists (or is planned) for a tenant to manage its own look.

The first HTTP file-upload endpoint in this codebase (`POST .../logo`,
`POST .../favicon`) - every other object-storage write today happens on an internal
pipeline path (`csense_shared.pipeline.evidence`), not through a real multipart
request. Mirrors that module's own `_store_object` shape (digest, upload, read-back
verify, `stored_objects` row) rather than inventing a second convention.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, get_app_settings, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext
from csense_shared.storage.objects import BUCKET_BRANDING, branding_asset_key, public_branding_url

router = APIRouter(
    prefix="/api/v1/admin/organizations/{organization_id}/branding", tags=["admin-branding"]
)

# Every reserved top-level path frontend/customer-crm/src/App.tsx declares, plus a
# handful of platform path prefixes a slug must never shadow (`api`/`ws`/`media` are
# Traefik PathPrefix roots; `admin`/`public` are reserved for this same reason even
# though no frontend route uses them today). Kept as a flat literal set rather than
# imported from the frontend build - the two are in different languages/packages, and
# this list changes rarely enough that a manual sync here is the simpler contract.
_RESERVED_SLUGS = frozenset(
    {
        "accept-invitation", "audit", "cameras", "child-tenants", "dashboard",
        "detections", "edge", "incidents", "login", "notification-policies",
        "pipelines", "recipient-groups", "rules", "settings", "sites", "team",
        "webhooks", "zones", "api", "admin", "public", "ws", "media",
    }
)

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
_MAX_ASSET_BYTES = 2 * 1024 * 1024  # logos/favicons have no business being bigger
_ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/svg+xml"}
_ALLOWED_FAVICON_TYPES = {"image/png", "image/x-icon", "image/vnd.microsoft.icon", "image/svg+xml"}
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
}


def _validate_slug(slug: str) -> None:
    if not _SLUG_PATTERN.match(slug):
        raise ApiError(
            status_code=422,
            code="invalid_slug",
            message="Slug must be lowercase letters, digits and hyphens, 2-49 characters, starting with a letter or digit.",
        )
    if slug in _RESERVED_SLUGS:
        raise ApiError(
            status_code=422,
            code="reserved_slug",
            message=f"'{slug}' is a reserved path and cannot be used as a brand slug.",
            details={"reserved": sorted(_RESERVED_SLUGS)},
        )


class BrandingOut(BaseModel):
    organization_id: str
    slug: str
    display_name: str
    logo_url: str | None
    favicon_url: str | None
    color_primary: str | None
    color_accent: str | None
    color_canvas: str | None
    email_from_name: str
    status: str


async def _load_branding_row(db: AsyncSession, organization_id: uuid.UUID) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT ob.organization_id, ob.slug, ob.display_name,
                       lo.bucket AS logo_bucket, lo.object_key AS logo_key,
                       fo.bucket AS favicon_bucket, fo.object_key AS favicon_key,
                       ob.color_primary, ob.color_accent, ob.color_canvas,
                       ob.email_from_name, ob.status
                FROM org_branding ob
                LEFT JOIN stored_objects lo ON lo.id = ob.logo_object_id
                LEFT JOIN stored_objects fo ON fo.id = ob.favicon_object_id
                WHERE ob.organization_id = :organization_id
                """
            ),
            {"organization_id": organization_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _to_branding_out(row: dict, settings: Settings) -> BrandingOut:
    return BrandingOut(
        organization_id=str(row["organization_id"]),
        slug=row["slug"],
        display_name=row["display_name"],
        logo_url=public_branding_url(settings, row["logo_key"]) if row["logo_key"] else None,
        favicon_url=public_branding_url(settings, row["favicon_key"]) if row["favicon_key"] else None,
        color_primary=row["color_primary"],
        color_accent=row["color_accent"],
        color_canvas=row["color_canvas"],
        email_from_name=row["email_from_name"],
        status=row["status"],
    )


@router.get("", response_model=BrandingOut)
async def get_branding(
    organization_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> BrandingOut:
    require_permission(context, "organization.branding.manage")
    row = await _load_branding_row(db, organization_id)
    if row is None:
        raise NotFoundError("No branding configured for this organization.")
    return _to_branding_out(row, settings)


class UpsertBrandingIn(BaseModel):
    slug: str = Field(min_length=2, max_length=49)
    display_name: str = Field(min_length=1, max_length=200)
    email_from_name: str = Field(min_length=1, max_length=200)
    color_primary: str | None = Field(default=None, max_length=32)
    color_accent: str | None = Field(default=None, max_length=32)
    color_canvas: str | None = Field(default=None, max_length=32)


@router.put("", response_model=BrandingOut, status_code=200)
async def upsert_branding(
    organization_id: uuid.UUID,
    body: UpsertBrandingIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> BrandingOut:
    require_permission(context, "organization.branding.manage")
    _validate_slug(body.slug)

    before = await _load_branding_row(db, organization_id)

    # No explicit commit/rollback here: `platform_db_session` (app/deps.py) already
    # wraps this whole request in `session.begin()`, which commits once this endpoint
    # returns normally and rolls back on any exception that propagates out of it - a
    # manual commit()/rollback() here would fight that managed transaction rather than
    # cooperate with it (confirmed against every other admin_api endpoint: none of them
    # call either).
    try:
        await db.execute(
            text(
                """
                INSERT INTO org_branding
                    (organization_id, slug, display_name, email_from_name,
                     color_primary, color_accent, color_canvas, created_by)
                VALUES
                    (:organization_id, :slug, :display_name, :email_from_name,
                     :color_primary, :color_accent, :color_canvas, :created_by)
                ON CONFLICT (organization_id) DO UPDATE SET
                    slug = EXCLUDED.slug,
                    display_name = EXCLUDED.display_name,
                    email_from_name = EXCLUDED.email_from_name,
                    color_primary = EXCLUDED.color_primary,
                    color_accent = EXCLUDED.color_accent,
                    color_canvas = EXCLUDED.color_canvas,
                    updated_at = now()
                """
            ),
            {
                "organization_id": organization_id,
                "slug": body.slug,
                "display_name": body.display_name,
                "email_from_name": body.email_from_name,
                "color_primary": body.color_primary,
                "color_accent": body.color_accent,
                "color_canvas": body.color_canvas,
                "created_by": context.developer_user_id,
            },
        )
    except Exception as exc:
        if "org_branding_slug_key" in str(exc):
            raise ApiError(
                status_code=409, code="slug_taken",
                message=f"Slug '{body.slug}' is already in use by another organization.",
            ) from exc
        raise

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="organization.branding.update",
        outcome="success",
        target_type="organization",
        target_id=str(organization_id),
        reason=f"Set branding slug='{body.slug}' display_name='{body.display_name}'",
        before_patch=before,
        after_patch=body.model_dump(),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="organization.branding.updated.v1",
        event_payload={"organization_id": str(organization_id), "slug": body.slug},
        aggregate_type="organization",
        aggregate_id=str(organization_id),
    )

    row = await _load_branding_row(db, organization_id)
    return _to_branding_out(row, settings)


async def _upload_asset(
    *,
    request: Request,
    db: AsyncSession,
    context: PlatformContext,
    organization_id: uuid.UUID,
    kind: str,
    file: UploadFile,
    allowed_types: set[str],
) -> None:
    require_permission(context, "organization.branding.manage")

    existing = await _load_branding_row(db, organization_id)
    if existing is None:
        raise NotFoundError(
            "Set the organization's slug/display_name via PUT before uploading an asset."
        )

    content_type = file.content_type or ""
    if content_type not in allowed_types:
        raise ApiError(
            status_code=422, code="unsupported_content_type",
            message=f"Unsupported content type '{content_type}'.",
            details={"allowed": sorted(allowed_types)},
        )

    payload = await file.read()
    if len(payload) > _MAX_ASSET_BYTES:
        raise ApiError(
            status_code=413, code="asset_too_large",
            message=f"File exceeds the {_MAX_ASSET_BYTES // (1024 * 1024)} MB limit.",
        )

    object_store = request.app.state.object_store
    if object_store is None:
        raise ApiError(
            status_code=503, code="object_store_unavailable",
            message="Object storage is not available right now.",
        )

    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    ext = _EXT_BY_MIME[content_type]
    object_key = branding_asset_key(organization_id, kind, digest, ext)

    object_store.put_object(
        BUCKET_BRANDING, object_key, io.BytesIO(payload), length=size, content_type=content_type,
    )
    stat = object_store.stat_object(BUCKET_BRANDING, object_key)
    if stat.size != size:
        raise ApiError(
            status_code=502, code="upload_verification_failed",
            message="Uploaded object did not verify against what was written.",
        )

    object_id = (
        await db.execute(
            text(
                """
                INSERT INTO stored_objects
                    (tenant_id, bucket, object_key, object_type, mime_type, size_bytes,
                     sha256, retention_class, created_by)
                VALUES (NULL, :bucket, :object_key, :object_type, :mime_type, :size,
                        :sha256, 'standard', :created_by)
                ON CONFLICT (bucket, object_key) DO UPDATE SET object_key = EXCLUDED.object_key
                RETURNING id
                """
            ),
            {
                "bucket": BUCKET_BRANDING,
                "object_key": object_key,
                "object_type": f"branding_{kind}",
                "mime_type": content_type,
                "size": size,
                "sha256": digest,
                "created_by": context.developer_user_id,
            },
        )
    ).scalar_one()

    column = "logo_object_id" if kind == "logo" else "favicon_object_id"
    await db.execute(
        text(f"UPDATE org_branding SET {column} = :object_id, updated_at = now() WHERE organization_id = :organization_id"),
        {"object_id": object_id, "organization_id": organization_id},
    )
    # No explicit commit - see upsert_branding's own note; platform_db_session's
    # session.begin() commits once the request returns normally.

    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action=f"organization.branding.{kind}_upload",
        outcome="success",
        target_type="organization",
        target_id=str(organization_id),
        reason=f"Uploaded {kind} ({size} bytes, {content_type})",
        before_patch=None,
        after_patch={"object_key": object_key, "sha256": digest},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type=f"organization.branding.{kind}_uploaded.v1",
        event_payload={"organization_id": str(organization_id), "object_key": object_key},
        aggregate_type="organization",
        aggregate_id=str(organization_id),
    )


@router.post("/logo", response_model=BrandingOut)
async def upload_logo(
    organization_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> BrandingOut:
    await _upload_asset(
        request=request, db=db, context=context, organization_id=organization_id,
        kind="logo", file=file, allowed_types=_ALLOWED_LOGO_TYPES,
    )
    row = await _load_branding_row(db, organization_id)
    return _to_branding_out(row, settings)


@router.post("/favicon", response_model=BrandingOut)
async def upload_favicon(
    organization_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> BrandingOut:
    await _upload_asset(
        request=request, db=db, context=context, organization_id=organization_id,
        kind="favicon", file=file, allowed_types=_ALLOWED_FAVICON_TYPES,
    )
    row = await _load_branding_row(db, organization_id)
    return _to_branding_out(row, settings)
