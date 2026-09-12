"""Camera management.

The whole surface is shaped by one rule: **a stored camera password is write-only.** There
is no field, no query parameter and no debug mode that returns it. Once set, it can be
replaced or deleted, never read back. Operators dislike this the first time they hit it,
and it is the single most valuable property here - a read-only flaw anywhere in the API,
or one over-broad role, cannot turn into a list of working camera credentials.

`has_credentials` is exposed instead, because the real question an operator has is "did
this save" rather than "what is it".

**Probing is a privileged, guarded action.** Verifying a camera makes the server open a
connection to an address the caller supplied, so it needs its own permission and it goes
through the SSRF guard, which resolves the hostname and refuses anything private,
loopback, link-local or reserved. See `csense_shared.security.outbound`.

**Stream paths are paths, never URLs.** A URL field would let someone store
`rtsp://user:pass@host/...` and put a credential straight into a column that gets logged
and listed. The database enforces the leading slash; this layer rejects the rest with a
message that explains why.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from csense_shared.cameras.health import classify_health_events
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.licensing import QuotaExceededError, require_license_not_restricted, reserve_quota
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import BlockedAddressError
from csense_shared.security.permissions import require_permission
from csense_shared.security.secret_store import delete_secret, write_secret
from csense_shared.security.tenant_context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/cameras", tags=["cameras"])

MAX_PAGE_SIZE = 100
CREDENTIAL_PURPOSE = "camera.rtsp"


class CameraIn(BaseModel):
    site_id: uuid.UUID
    zone_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    vendor: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    hostname: str | None = Field(default=None, max_length=253)
    rtsp_port: int | None = Field(default=554, ge=1, le=65535)
    main_stream_path: str | None = Field(default=None, max_length=300)
    sub_stream_path: str | None = Field(default=None, max_length=300)
    username: str | None = Field(default=None, max_length=120)
    rtsp_transport: str = Field(default="tcp", pattern="^(tcp|udp)$")

    @field_validator("main_stream_path", "sub_stream_path")
    @classmethod
    def _must_be_a_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "://" in value or "@" in value:
            raise ValueError(
                "Give the stream path only (e.g. /unicast/c1/s0/live), not a full URL. "
                "A URL can carry a username and password, and credentials belong in the "
                "encrypted store rather than in a column that gets listed and logged."
            )
        if not value.startswith("/"):
            raise ValueError("A stream path must start with '/'.")
        return value


class CameraPatch(BaseModel):
    """Every field optional; absent means unchanged.

    Note what is *not* here: `password`. Changing a credential is a separate endpoint
    behind a separate permission, so it cannot ride along inside a routine edit.
    """

    zone_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    vendor: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    hostname: str | None = Field(default=None, max_length=253)
    rtsp_port: int | None = Field(default=None, ge=1, le=65535)
    main_stream_path: str | None = Field(default=None, max_length=300)
    sub_stream_path: str | None = Field(default=None, max_length=300)
    username: str | None = Field(default=None, max_length=120)
    rtsp_transport: str | None = Field(default=None, pattern="^(tcp|udp)$")
    status: str | None = Field(default=None, pattern="^(provisioning|ready|disabled)$")

    _validate_paths = field_validator("main_stream_path", "sub_stream_path")(
        CameraIn._must_be_a_path.__func__
    )


class CredentialIn(BaseModel):
    """The only way a camera password enters the system."""

    username: str | None = Field(default=None, max_length=120)
    password: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=120)


class CameraOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    site_name: str | None = None
    zone_id: uuid.UUID | None = None
    name: str
    code: str
    vendor: str | None = None
    model: str | None = None
    hostname: str | None = None
    rtsp_port: int | None = None
    main_stream_path: str | None = None
    sub_stream_path: str | None = None
    username: str | None = None
    rtsp_transport: str
    status: str
    # Whether a password is stored - never the password. The question an operator actually
    # has is "did that save", and this answers it without the value ever leaving the
    # database.
    has_credentials: bool
    stream_profile: dict = {}
    last_probed_at: dt.datetime | None = None
    last_frame_at: dt.datetime | None = None
    last_error: str | None = None
    created_at: dt.datetime


_SELECT = """
    SELECT c.id, c.site_id, s.name, c.zone_id, c.name, c.code, c.vendor, c.model,
           c.hostname, c.rtsp_port, c.main_stream_path, c.sub_stream_path, c.username,
           c.rtsp_transport, c.status::text, (c.endpoint_secret_id IS NOT NULL),
           c.stream_profile, c.last_probed_at, c.last_frame_at, c.last_error, c.created_at
    FROM cameras c
    LEFT JOIN sites s ON s.id = c.site_id
"""


def _to_camera(row) -> CameraOut:
    return CameraOut(
        id=row[0], site_id=row[1], site_name=row[2], zone_id=row[3], name=row[4],
        code=row[5], vendor=row[6], model=row[7], hostname=row[8], rtsp_port=row[9],
        main_stream_path=row[10], sub_stream_path=row[11], username=row[12],
        rtsp_transport=row[13], status=row[14], has_credentials=row[15],
        stream_profile=row[16] or {}, last_probed_at=row[17], last_frame_at=row[18],
        last_error=row[19], created_at=row[20],
    )


async def load_camera(db: AsyncSession, camera_id: uuid.UUID) -> CameraOut:
    """Public (not `_load`) - `media.py`'s live-session endpoints reuse this rather than
    re-deriving the same RLS-scoped "not found covers both missing and another tenant's
    camera" lookup."""
    row = (
        await db.execute(
            text(_SELECT + " WHERE c.id = :id AND c.deleted_at IS NULL"),
            {"id": camera_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")
    return _to_camera(row)


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    site_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(provisioning|ready|disabled)$"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[CameraOut]:
    require_permission(context, "camera.read")

    clauses = ["c.deleted_at IS NULL"]
    params: dict = {"limit": limit}
    if site_id:
        clauses.append("c.site_id = :site_id")
        params["site_id"] = site_id
    if status:
        clauses.append("c.status = CAST(:status AS camera_status)")
        params["status"] = status

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY c.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_camera(row) for row in rows]


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(
    camera_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraOut:
    require_permission(context, "camera.read")
    return await load_camera(db, camera_id)


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(
    body: CameraIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraOut:
    require_permission(context, "camera.create")

    site = (
        await db.execute(
            text("SELECT 1 FROM sites WHERE id = :id"), {"id": body.site_id}
        )
    ).first()
    if site is None:
        # Row-level security already scopes this, so a missing row means either "no such
        # site" or "not yours" - and those must be indistinguishable, or the API becomes a
        # way to enumerate other tenants' site ids.
        raise NotFoundError("No such site.")

    existing = (
        await db.execute(
            text("SELECT 1 FROM cameras WHERE code = :code AND deleted_at IS NULL"),
            {"code": body.code},
        )
    ).first()
    if existing:
        raise ApiError(
            status_code=409,
            code="camera_code_taken",
            message=f"A camera with code '{body.code}' already exists.",
        )

    # An expired/suspended/revoked license is a hard stop on creating new resources -
    # checked before the quota reservation below, since a tenant past that point should
    # see "your license lapsed" rather than a quota-shaped error that suggests raising a
    # limit would help. A tenant with no license at all, or one still in its grace
    # window, passes through unaffected - see require_license_not_restricted's own
    # docstring for why.
    await require_license_not_restricted(db, tenant_id=context.tenant_id)

    # A tenant with no camera.count entitlement (no license, or a license that doesn't
    # cap this) is unlimited - reserve_quota no-ops in that case (see its own docstring).
    # A tenant that does have one and is at it gets a real, in-transaction rejection here,
    # not a silent over-allocation - the same INSERT below never runs on that path.
    try:
        await reserve_quota(db, tenant_id=context.tenant_id, quota_code="camera.count", quantity=1)
    except QuotaExceededError as exc:
        raise ApiError(
            status_code=402,
            code="quota_exceeded",
            message=(
                f"This tenant's camera limit ({exc.limit_value}) is already in use "
                f"({exc.in_use}). Contact your reseller or AIRIVU to increase it."
            ),
        ) from exc

    camera_id = (
        await db.execute(
            text(
                """
                INSERT INTO cameras
                    (tenant_id, site_id, zone_id, name, code, vendor, model, hostname,
                     rtsp_port, main_stream_path, sub_stream_path, username,
                     rtsp_transport, status)
                VALUES (:tenant_id, :site_id, :zone_id, :name, :code, :vendor, :model,
                        :hostname, :rtsp_port, :main_stream_path, :sub_stream_path,
                        :username, :rtsp_transport, 'provisioning')
                RETURNING id
                """
            ),
            {"tenant_id": context.tenant_id, **body.model_dump()},
        )
    ).scalar_one()

    logger.info(
        "camera_created",
        extra={"camera_id": str(camera_id), "tenant_id": str(context.tenant_id)},
    )
    return await load_camera(db, camera_id)


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: uuid.UUID,
    body: CameraPatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraOut:
    require_permission(context, "camera.manage")
    await load_camera(db, camera_id)  # 404s before doing anything

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await load_camera(db, camera_id)

    assignments = ", ".join(
        f"{field} = CAST(:{field} AS camera_status)" if field == "status" else f"{field} = :{field}"
        for field in changes
    )
    await db.execute(
        text(
            f"UPDATE cameras SET {assignments}, updated_at = now(), version = version + 1 "
            "WHERE id = :id AND deleted_at IS NULL"
        ),
        {"id": camera_id, **changes},
    )
    logger.info(
        "camera_updated",
        extra={"camera_id": str(camera_id), "fields": sorted(changes)},
    )
    return await load_camera(db, camera_id)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(
    camera_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Soft-deletes the camera and hard-deletes its credential.

    The row is kept because incidents and evidence reference it, and a listing that says
    "camera deleted" is more useful than a dangling id. The *credential* is not kept:
    there is no reason to retain a password for a camera nobody will connect to, and
    keeping one is how a decommissioned device stays exploitable.
    """
    require_permission(context, "camera.manage")
    row = (
        await db.execute(
            text(
                "SELECT endpoint_secret_id FROM cameras "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": camera_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")

    await db.execute(
        text(
            "UPDATE cameras SET deleted_at = now(), status = 'disabled', "
            "endpoint_secret_id = NULL, updated_at = now() WHERE id = :id"
        ),
        {"id": camera_id},
    )
    if row[0]:
        await delete_secret(db, secret_id=row[0])

    logger.info("camera_deleted", extra={"camera_id": str(camera_id)})


@router.put("/{camera_id}/credentials", response_model=CameraOut)
async def set_credentials(
    camera_id: uuid.UUID,
    body: CredentialIn,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraOut:
    """Stores the password this camera connects with. Write-only, by design.

    Behind its own permission rather than `camera.manage`: renaming a camera is routine
    work, replacing the credential to a device that watches people is not.
    """
    require_permission(context, "camera.credential.manage")
    camera = await load_camera(db, camera_id)

    keyring = keyring_from_settings(request.app.state.settings)
    try:
        secret_id = await write_secret(
            db,
            keyring,
            tenant_id=context.tenant_id,
            purpose=CREDENTIAL_PURPOSE,
            plaintext=body.password,
            label=body.label or f"{camera.name} RTSP",
        )
    except EnvelopeError as exc:
        # The message describes the failure, never the value.
        raise ApiError(
            status_code=500, code="credential_store_failed", message=str(exc)
        ) from exc

    await db.execute(
        text(
            "UPDATE cameras SET endpoint_secret_id = :secret_id, "
            "username = COALESCE(:username, username), updated_at = now() WHERE id = :id"
        ),
        {"id": camera_id, "secret_id": secret_id, "username": body.username},
    )

    logger.info(
        "camera_credentials_set",
        extra={
            "camera_id": str(camera_id),
            "actor": str(context.user_id),
            # No length, no prefix: a length alone narrows a brute force.
        },
    )
    return await load_camera(db, camera_id)


@router.delete("/{camera_id}/credentials", status_code=204)
async def clear_credentials(
    camera_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "camera.credential.manage")
    row = (
        await db.execute(
            text(
                "SELECT endpoint_secret_id FROM cameras "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": camera_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")

    await db.execute(
        text("UPDATE cameras SET endpoint_secret_id = NULL, updated_at = now() "
             "WHERE id = :id"),
        {"id": camera_id},
    )
    if row[0]:
        await delete_secret(db, secret_id=row[0])
    logger.info("camera_credentials_cleared", extra={"camera_id": str(camera_id)})


class ProbeResult(BaseModel):
    reachable: bool
    detail: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    framerate: float | None = None
    transport: str | None = None
    elapsed_ms: int | None = None


@router.post("/{camera_id}/probe", response_model=ProbeResult)
async def probe_camera(
    camera_id: uuid.UUID,
    request: Request,
    stream: str = Query(default="main", pattern="^(main|sub)$"),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> ProbeResult:
    """Connects to the camera and records what the stream actually is.

    Behind its own permission because it makes the server open an outbound connection to
    an address the tenant chose, and through the SSRF guard because that address could
    otherwise be the metadata service or an internal database.
    """
    require_permission(context, "camera.probe")
    camera = await load_camera(db, camera_id)

    path = camera.main_stream_path if stream == "main" else camera.sub_stream_path
    if not camera.hostname or not path:
        raise ApiError(
            status_code=422,
            code="camera_not_configured",
            message=(
                f"This camera has no hostname or {stream} stream path set, so there is "
                "nothing to connect to."
            ),
        )

    from app.services.camera_probe import probe_stream

    try:
        result = await probe_stream(
            request.app.state.settings,
            db,
            tenant_id=context.tenant_id,
            camera_id=camera_id,
            hostname=camera.hostname,
            port=camera.rtsp_port or 554,
            path=path,
            username=camera.username,
            secret_purpose=CREDENTIAL_PURPOSE,
        )
    except BlockedAddressError as exc:
        # 422, not 500: the request is well-formed but names an address we refuse. Saying
        # so plainly is better than a generic failure an operator cannot act on.
        raise ApiError(
            status_code=422, code="address_not_permitted", message=str(exc)
        ) from exc

    await db.execute(
        text(
            "UPDATE cameras SET last_probed_at = now(), stream_profile = CAST(:profile AS jsonb), "
            "last_error = :error, "
            "status = CASE WHEN :ok AND status = 'provisioning' THEN 'ready' ELSE status END "
            # `deleted_at IS NULL`: found by a real pentest run - without it, a camera
            # soft-deleted between `load_camera()`'s fetch above and this write (a real,
            # if narrow, race - another request's DELETE landing mid-probe) would still
            # get its telemetry updated after deletion. Same guard `_require_own_camera`
            # already applies everywhere else a camera is looked up.
            "WHERE id = :id AND deleted_at IS NULL"
        ),
        {
            "id": camera_id,
            "profile": result.profile_json(),
            "error": None if result.reachable else result.detail[:500],
            "ok": result.reachable,
        },
    )
    # Durable telemetry history (CHECKLIST: "Camera health current-state model +
    # telemetry history") - `cameras.last_probed_at`/`last_error`/`stream_profile`
    # (migration 0020) already answer "right now"; every probe overwrites that single
    # row, so this is the only place "over time" is answered from.
    events = classify_health_events(
        reachable=result.reachable, detail=result.detail,
        elapsed_ms=result.elapsed_ms, framerate=result.framerate,
    )
    for event in events:
        await db.execute(
            text(
                "INSERT INTO camera_health_events "
                "(tenant_id, camera_id, status, check_name, detail, codec, width, height, framerate) "
                "VALUES (:tenant_id, :camera_id, CAST(:status AS camera_health_status), :check_name, "
                ":detail, :codec, :width, :height, :framerate)"
            ),
            {
                "tenant_id": context.tenant_id, "camera_id": camera_id,
                "status": event["status"], "check_name": event["check_name"], "detail": event["detail"],
                "codec": result.codec, "width": result.width, "height": result.height,
                "framerate": result.framerate,
            },
        )
    return ProbeResult(
        reachable=result.reachable,
        detail=result.detail,
        codec=result.codec,
        width=result.width,
        height=result.height,
        framerate=result.framerate,
        transport=result.transport,
        elapsed_ms=result.elapsed_ms,
    )


class CameraHealthEvent(BaseModel):
    status: str
    check_name: str | None
    detail: str | None
    codec: str | None
    width: int | None
    height: int | None
    framerate: float | None
    occurred_at: dt.datetime


class CameraHealthOut(BaseModel):
    # Derived from cameras.last_probed_at/last_error (migration 0020), not a second
    # stored current-state - see migration 0041's own docstring for why duplicating it
    # into camera_health_events would just be two places for the same fact to disagree.
    current_status: str
    last_probed_at: dt.datetime | None
    last_error: str | None
    # CHECKLIST "Camera health use cases": which named checks are currently degraded/
    # failed, derived from each check_name's own most recent event within `history` -
    # not a separate stored value, for the same "one place this fact can disagree with
    # itself" reasoning current_status already follows. Empty when everything's clean,
    # including for a camera whose events predate check_name (NULL) - those cannot
    # contribute a named concern, only the binary current_status already covers them.
    active_concerns: list[str]
    history: list[CameraHealthEvent]


@router.get("/{camera_id}/health", response_model=CameraHealthOut)
async def get_camera_health(
    camera_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CameraHealthOut:
    require_permission(context, "camera.read")

    camera_row = (
        await db.execute(
            text("SELECT last_probed_at, last_error FROM cameras WHERE id = :id AND deleted_at IS NULL"),
            {"id": camera_id},
        )
    ).first()
    if camera_row is None:
        raise NotFoundError("No such camera.")
    last_probed_at, last_error = camera_row
    current_status = "unknown" if last_probed_at is None else ("offline" if last_error else "online")

    rows = (
        await db.execute(
            text(
                "SELECT status::text, check_name, detail, codec, width, height, framerate, occurred_at "
                "FROM camera_health_events WHERE camera_id = :camera_id "
                "ORDER BY occurred_at DESC LIMIT :limit"
            ),
            {"camera_id": camera_id, "limit": limit},
        )
    ).all()

    # Rows arrive newest-first, so the first time a given check_name is seen here is that
    # check's own most recent event.
    latest_by_check: dict[str, str] = {}
    for r in rows:
        check_name, status = r[1], r[0]
        if check_name is not None and check_name not in latest_by_check:
            latest_by_check[check_name] = status
    active_concerns = sorted(name for name, status in latest_by_check.items() if status != "online")

    return CameraHealthOut(
        current_status=current_status, last_probed_at=last_probed_at, last_error=last_error,
        active_concerns=active_concerns,
        history=[
            CameraHealthEvent(
                status=r[0], check_name=r[1], detail=r[2], codec=r[3], width=r[4], height=r[5],
                framerate=float(r[6]) if r[6] is not None else None, occurred_at=r[7],
            )
            for r in rows
        ],
    )
