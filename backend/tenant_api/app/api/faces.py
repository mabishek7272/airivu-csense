"""Face registration: the people a tenant has enrolled for face recognition.

Biometric data, so the shape is deliberately strict (schema rationale in migration 0064):

  **Consent is required to create a person.** `consent` must be true and is recorded with a
  timestamp and a free-text note; there is no path to a registered person without it.

  **Photos are never stored.** An uploaded photo is sent to ai-runtime, which returns a
  normalised 512-d embedding; only that embedding is kept, encrypted with the same AES-GCM
  envelope camera passwords use (purpose `face.template`). The image itself is discarded.

  **Removal is real removal.** Deleting a person deletes every template's encrypted secret
  in the same transaction, not just the rows that point at them.

  **Every change is audited** with the acting user, so "who registered this person, and
  when" stays answerable.

Only tenant_owner and tenant_operator hold `face.manage`; anyone in the tenant can read the
list of names (`face.read`). Embeddings are never returned by any endpoint here.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.faces.matching import TemplateError, encode_template
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.permissions import require_permission
from csense_shared.security.secret_store import delete_secret, write_secret
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/faces", tags=["faces"])

TEMPLATE_PURPOSE = "face.template"
MAX_PHOTOS_PER_REQUEST = 5
MAX_TEMPLATES_PER_PERSON = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# A face smaller than this in the source photo gives a poor embedding - rejected up front
# rather than stored and then silently failing to match later.
MIN_FACE_PIXELS = 80
MIN_DETECTOR_CONFIDENCE = 0.60


class IdentityIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    external_ref: str | None = Field(default=None, max_length=120)
    list_type: str = Field(default="known", pattern="^(known|watchlist)$")
    consent: bool
    consent_note: str = Field(min_length=3, max_length=500)


class IdentityPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    external_ref: str | None = Field(default=None, max_length=120)
    list_type: str | None = Field(default=None, pattern="^(known|watchlist)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class IdentityOut(BaseModel):
    id: uuid.UUID
    display_name: str
    external_ref: str | None = None
    list_type: str
    status: str
    template_count: int
    consent_recorded_at: dt.datetime
    consent_note: str
    created_at: dt.datetime


_SELECT = """
    SELECT i.id, i.display_name, i.external_ref, i.list_type, i.status,
           (SELECT count(*) FROM face_templates t WHERE t.identity_id = i.id),
           i.consent_recorded_at, i.consent_note, i.created_at
    FROM face_identities i
"""


def _to_out(row) -> IdentityOut:
    return IdentityOut(
        id=row[0], display_name=row[1], external_ref=row[2], list_type=row[3],
        status=row[4], template_count=row[5], consent_recorded_at=row[6],
        consent_note=row[7], created_at=row[8],
    )


async def _load(db: AsyncSession, identity_id: uuid.UUID) -> IdentityOut:
    row = (await db.execute(text(_SELECT + " WHERE i.id = :id"), {"id": identity_id})).first()
    if row is None:
        raise NotFoundError("No such person.")
    return _to_out(row)


def _audit(context: TenantContext, action: str, target_id: uuid.UUID, **patch):
    return dict(
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action=action,
        outcome="success",
        target_type="face_identity",
        target_id=str(target_id),
        after_patch=patch or None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )


@router.get("/identities", response_model=list[IdentityOut])
async def list_identities(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[IdentityOut]:
    require_permission(context, "face.read")
    rows = (await db.execute(text(_SELECT + " ORDER BY i.display_name LIMIT 500"))).all()
    return [_to_out(r) for r in rows]


@router.post("/identities", response_model=IdentityOut, status_code=201)
async def create_identity(
    body: IdentityIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> IdentityOut:
    require_permission(context, "face.manage")
    if not body.consent:
        raise ApiError(
            status_code=422,
            code="consent_required",
            message=(
                "A person can only be registered for face recognition once they have "
                "agreed. Confirm consent, and note how it was given."
            ),
        )
    if body.external_ref:
        clash = (
            await db.execute(
                text("SELECT 1 FROM face_identities WHERE external_ref = :r"),
                {"r": body.external_ref},
            )
        ).first()
        if clash:
            raise ApiError(
                status_code=409, code="external_ref_taken",
                message=f"Someone is already registered with reference '{body.external_ref}'.",
            )

    identity_id = (
        await db.execute(
            text(
                """
                INSERT INTO face_identities
                    (tenant_id, display_name, external_ref, list_type,
                     consent_recorded_at, consent_note, created_by)
                VALUES (:tenant_id, :name, :ref, :list_type, now(), :note, :created_by)
                RETURNING id
                """
            ),
            {
                "tenant_id": context.tenant_id, "name": body.display_name,
                "ref": body.external_ref, "list_type": body.list_type,
                "note": body.consent_note, "created_by": context.user_id,
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db, **_audit(context, "face.identity.create", identity_id,
                     display_name=body.display_name, list_type=body.list_type),
    )
    return await _load(db, identity_id)


@router.patch("/identities/{identity_id}", response_model=IdentityOut)
async def update_identity(
    identity_id: uuid.UUID,
    body: IdentityPatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> IdentityOut:
    require_permission(context, "face.manage")
    await _load(db, identity_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await _load(db, identity_id)
    assignments = ", ".join(f"{k} = :{k}" for k in changes)
    await db.execute(
        text(f"UPDATE face_identities SET {assignments}, updated_at = now() WHERE id = :id"),
        {"id": identity_id, **changes},
    )
    await record_audit_and_outbox(db, **_audit(context, "face.identity.update", identity_id, **changes))
    return await _load(db, identity_id)


@router.delete("/identities/{identity_id}", status_code=204)
async def delete_identity(
    identity_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Removes the person and destroys every stored template (the encrypted secrets
    included - the FK cascade alone would leave those behind)."""
    require_permission(context, "face.manage")
    person = await _load(db, identity_id)

    secret_ids = [
        r[0] for r in (
            await db.execute(
                text("SELECT secret_id FROM face_templates WHERE identity_id = :id"),
                {"id": identity_id},
            )
        ).all()
    ]
    await db.execute(text("DELETE FROM face_identities WHERE id = :id"), {"id": identity_id})
    for secret_id in secret_ids:
        await delete_secret(db, secret_id=secret_id)

    await record_audit_and_outbox(
        db, **_audit(context, "face.identity.delete", identity_id,
                     display_name=person.display_name, templates_destroyed=len(secret_ids)),
    )


class PhotosOut(BaseModel):
    added: int
    template_count: int


@router.post("/identities/{identity_id}/photos", response_model=PhotosOut, status_code=201)
async def add_photos(
    identity_id: uuid.UUID,
    request: Request,
    photos: list[UploadFile] = File(...),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> PhotosOut:
    """Registers 1-5 photos of the person. Each must show exactly one clear face. The photo
    is embedded and discarded; only the encrypted embedding is kept."""
    require_permission(context, "face.manage")
    person = await _load(db, identity_id)
    if person.status != "active":
        raise ApiError(status_code=409, code="person_disabled", message="This person is disabled.")
    if not 1 <= len(photos) <= MAX_PHOTOS_PER_REQUEST:
        raise ApiError(
            status_code=422, code="photo_count",
            message=f"Send between 1 and {MAX_PHOTOS_PER_REQUEST} photos at a time.",
        )
    if person.template_count + len(photos) > MAX_TEMPLATES_PER_PERSON:
        raise ApiError(
            status_code=409, code="too_many_templates",
            message=f"A person can have at most {MAX_TEMPLATES_PER_PERSON} registered photos.",
        )

    settings = request.app.state.settings
    keyring = keyring_from_settings(settings)
    added = 0

    async with httpx.AsyncClient(
        base_url=settings.ai_runtime_base_url, timeout=settings.ai_runtime_infer_timeout_seconds
    ) as client:
        for index, photo in enumerate(photos, start=1):
            data = await photo.read()
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ApiError(
                    status_code=422, code="bad_photo",
                    message=f"Photo {index} is empty or larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
                )
            try:
                response = await client.post(
                    "/internal/v1/infer-face",
                    data={"confidence": str(MIN_DETECTOR_CONFIDENCE)},
                    files={"frame": (photo.filename or "photo.jpg", data, photo.content_type or "image/jpeg")},
                )
            except httpx.HTTPError as exc:
                raise ApiError(
                    status_code=502, code="face_service_unavailable",
                    message="The face recognition service could not be reached. Try again shortly.",
                ) from exc
            if response.status_code != 200:
                raise ApiError(
                    status_code=502, code="face_service_error",
                    message="The face recognition service could not process that photo.",
                )
            result = response.json()
            faces = result["faces"]
            if len(faces) != 1:
                raise ApiError(
                    status_code=422, code="one_face_required",
                    message=(
                        f"Photo {index} shows {len(faces)} faces. Use a photo of just this "
                        "person, looking at the camera."
                        if faces else
                        f"No clear face was found in photo {index}. Use a well-lit photo "
                        "with the face looking at the camera."
                    ),
                )
            face = faces[0]
            width_px = (face["bbox"][2] - face["bbox"][0]) * result["frame_size"][0]
            if width_px < MIN_FACE_PIXELS:
                raise ApiError(
                    status_code=422, code="face_too_small",
                    message=f"The face in photo {index} is too small ({int(width_px)} px). Move closer or crop the photo.",
                )

            try:
                encoded = encode_template(face["embedding"])
                secret_id = await write_secret(
                    db, keyring, tenant_id=context.tenant_id, purpose=TEMPLATE_PURPOSE,
                    plaintext=encoded, label=f"face template {person.display_name}",
                )
            except (TemplateError, EnvelopeError) as exc:
                raise ApiError(status_code=500, code="template_store_failed", message=str(exc)) from exc

            await db.execute(
                text(
                    "INSERT INTO face_templates (tenant_id, identity_id, secret_id, detector_confidence) "
                    "VALUES (:tenant_id, :identity_id, :secret_id, :conf)"
                ),
                {
                    "tenant_id": context.tenant_id, "identity_id": identity_id,
                    "secret_id": secret_id, "conf": face["detector_confidence"],
                },
            )
            added += 1

    await record_audit_and_outbox(db, **_audit(context, "face.template.add", identity_id, photos=added))
    return PhotosOut(added=added, template_count=person.template_count + added)
