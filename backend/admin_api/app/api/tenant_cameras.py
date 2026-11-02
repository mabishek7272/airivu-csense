"""Admin-side mirror of `tenant_api/app/api/cameras.py` — see `tenant_sites.py`'s module
docstring for why this exists, and its warning about explicit `tenant_id` filtering
applying to every query here too (a `platform_session` sets no tenant scope for RLS to
filter by on its own).

Same rules as the original: a stored camera password is write-only, probing goes through
the SSRF guard, stream paths are paths never URLs. `probe_stream` now lives in
`csense_shared.cameras.probe` specifically so this file and the tenant-facing original can
share the same hand-rolled RTSP/digest-auth exchange rather than each carrying their own
copy of security-sensitive code.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.cameras.health import classify_health_events
from csense_shared.cameras.probe import probe_stream
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.licensing import QuotaExceededError, require_license_not_restricted, reserve_quota
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import BlockedAddressError
from csense_shared.security.permissions import require_permission
from csense_shared.security.secret_store import delete_secret, write_secret
from csense_shared.security.tenant_context import PlatformContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}/cameras", tags=["admin-tenant-cameras"])

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


async def load_camera(db: AsyncSession, tenant_id: uuid.UUID, camera_id: uuid.UUID) -> CameraOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE c.id = :id AND c.tenant_id = :tenant_id AND c.deleted_at IS NULL"),
            {"id": camera_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")
    return _to_camera(row)


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(provisioning|ready|disabled)$"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[CameraOut]:
    require_permission(context, "tenant.infra.manage")
    clauses = ["c.tenant_id = :tenant_id", "c.deleted_at IS NULL"]
    params: dict = {"tenant_id": tenant_id, "limit": limit}
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
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CameraOut:
    require_permission(context, "tenant.infra.manage")
    return await load_camera(db, tenant_id, camera_id)


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(
    tenant_id: uuid.UUID,
    body: CameraIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CameraOut:
    require_permission(context, "tenant.infra.manage")

    site = (
        await db.execute(
            text("SELECT 1 FROM sites WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"),
            {"id": body.site_id, "tenant_id": tenant_id},
        )
    ).first()
    if site is None:
        raise NotFoundError("No such site.")

    existing = (
        await db.execute(
            text("SELECT 1 FROM cameras WHERE tenant_id = :tenant_id AND code = :code AND deleted_at IS NULL"),
            {"tenant_id": tenant_id, "code": body.code},
        )
    ).first()
    if existing:
        raise ApiError(
            status_code=409,
            code="camera_code_taken",
            message=f"A camera with code '{body.code}' already exists for this tenant.",
        )

    await require_license_not_restricted(db, tenant_id=tenant_id)
    try:
        await reserve_quota(db, tenant_id=tenant_id, quota_code="camera.count", quantity=1)
    except QuotaExceededError as exc:
        raise ApiError(
            status_code=402,
            code="quota_exceeded",
            message=(
                f"This tenant's camera limit ({exc.limit_value}) is already in use "
                f"({exc.in_use}). Increase it before adding another."
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
            {"tenant_id": tenant_id, **body.model_dump()},
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_camera.create",
        outcome="success",
        target_type="camera",
        target_id=str(camera_id),
        after_patch={"name": body.name, "code": body.code, "hostname": body.hostname},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_camera_created", extra={"camera_id": str(camera_id), "tenant_id": str(tenant_id)})
    return await load_camera(db, tenant_id, camera_id)


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    body: CameraPatch,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CameraOut:
    require_permission(context, "tenant.infra.manage")
    await load_camera(db, tenant_id, camera_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await load_camera(db, tenant_id, camera_id)

    assignments = ", ".join(
        f"{field} = CAST(:{field} AS camera_status)" if field == "status" else f"{field} = :{field}"
        for field in changes
    )
    await db.execute(
        text(
            f"UPDATE cameras SET {assignments}, updated_at = now(), version = version + 1 "
            "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
        ),
        {"id": camera_id, "tenant_id": tenant_id, **changes},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_camera.update",
        outcome="success",
        target_type="camera",
        target_id=str(camera_id),
        after_patch=changes,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_camera_updated", extra={"camera_id": str(camera_id), "fields": sorted(changes)})
    return await load_camera(db, tenant_id, camera_id)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    row = (
        await db.execute(
            text(
                "SELECT endpoint_secret_id FROM cameras "
                "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
            ),
            {"id": camera_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")

    await db.execute(
        text(
            "UPDATE cameras SET deleted_at = now(), status = 'disabled', "
            "endpoint_secret_id = NULL, updated_at = now() WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": camera_id, "tenant_id": tenant_id},
    )
    if row[0]:
        await delete_secret(db, secret_id=row[0])

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_camera.delete",
        outcome="success",
        target_type="camera",
        target_id=str(camera_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_camera_deleted", extra={"camera_id": str(camera_id)})


@router.put("/{camera_id}/credentials", response_model=CameraOut)
async def set_credentials(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    body: CredentialIn,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CameraOut:
    require_permission(context, "tenant.infra.manage")
    camera = await load_camera(db, tenant_id, camera_id)

    keyring = keyring_from_settings(request.app.state.settings)
    try:
        secret_id = await write_secret(
            db,
            keyring,
            tenant_id=tenant_id,
            purpose=CREDENTIAL_PURPOSE,
            plaintext=body.password,
            label=body.label or f"{camera.name} RTSP",
        )
    except EnvelopeError as exc:
        raise ApiError(status_code=500, code="credential_store_failed", message=str(exc)) from exc

    await db.execute(
        text(
            "UPDATE cameras SET endpoint_secret_id = :secret_id, "
            "username = COALESCE(:username, username), updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": camera_id, "tenant_id": tenant_id, "secret_id": secret_id, "username": body.username},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_camera.credentials_set",
        outcome="success",
        target_type="camera",
        target_id=str(camera_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_camera_credentials_set", extra={"camera_id": str(camera_id)})
    return await load_camera(db, tenant_id, camera_id)


@router.delete("/{camera_id}/credentials", status_code=204)
async def clear_credentials(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    row = (
        await db.execute(
            text(
                "SELECT endpoint_secret_id FROM cameras "
                "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
            ),
            {"id": camera_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")

    await db.execute(
        text(
            "UPDATE cameras SET endpoint_secret_id = NULL, updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": camera_id, "tenant_id": tenant_id},
    )
    if row[0]:
        await delete_secret(db, secret_id=row[0])
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_camera.credentials_cleared",
        outcome="success",
        target_type="camera",
        target_id=str(camera_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_camera_credentials_cleared", extra={"camera_id": str(camera_id)})


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
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    request: Request,
    stream: str = Query(default="main", pattern="^(main|sub)$"),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ProbeResult:
    require_permission(context, "tenant.infra.manage")
    camera = await load_camera(db, tenant_id, camera_id)

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

    try:
        result = await probe_stream(
            request.app.state.settings,
            db,
            tenant_id=tenant_id,
            camera_id=camera_id,
            hostname=camera.hostname,
            port=camera.rtsp_port or 554,
            path=path,
            username=camera.username,
            secret_purpose=CREDENTIAL_PURPOSE,
        )
    except BlockedAddressError as exc:
        raise ApiError(status_code=422, code="address_not_permitted", message=str(exc)) from exc

    await db.execute(
        text(
            "UPDATE cameras SET last_probed_at = now(), stream_profile = CAST(:profile AS jsonb), "
            "last_error = :error, "
            "status = CASE WHEN :ok AND status = 'provisioning' THEN 'ready' ELSE status END "
            "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
        ),
        {
            "id": camera_id, "tenant_id": tenant_id,
            "profile": result.profile_json(),
            "error": None if result.reachable else result.detail[:500],
            "ok": result.reachable,
        },
    )
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
                "tenant_id": tenant_id, "camera_id": camera_id,
                "status": event["status"], "check_name": event["check_name"], "detail": event["detail"],
                "codec": result.codec, "width": result.width, "height": result.height,
                "framerate": result.framerate,
            },
        )
    logger.info("admin_camera_probed", extra={"camera_id": str(camera_id), "reachable": result.reachable})
    return ProbeResult(
        reachable=result.reachable, detail=result.detail, codec=result.codec,
        width=result.width, height=result.height, framerate=result.framerate,
        transport=result.transport, elapsed_ms=result.elapsed_ms,
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
    current_status: str
    last_probed_at: dt.datetime | None
    last_error: str | None
    active_concerns: list[str]
    history: list[CameraHealthEvent]


@router.get("/{camera_id}/health", response_model=CameraHealthOut)
async def get_camera_health(
    tenant_id: uuid.UUID,
    camera_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CameraHealthOut:
    require_permission(context, "tenant.infra.manage")

    camera_row = (
        await db.execute(
            text(
                "SELECT last_probed_at, last_error FROM cameras "
                "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
            ),
            {"id": camera_id, "tenant_id": tenant_id},
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
                "FROM camera_health_events WHERE camera_id = :camera_id AND tenant_id = :tenant_id "
                "ORDER BY occurred_at DESC LIMIT :limit"
            ),
            {"camera_id": camera_id, "tenant_id": tenant_id, "limit": limit},
        )
    ).all()

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
