"""Admin-side mirror of the **human-facing** subset of `tenant_api/app/api/edge.py` — see
`tenant_sites.py`'s module docstring for why this exists and its warning about explicit
`tenant_id` filtering (a `platform_session` sets no tenant scope for RLS to filter by).

**Deliberately excludes** `enrol`, `heartbeat`, `commands/pending`, and `commands/{id}/ack`
— those are called by the edge agent itself, authenticated with its own device credential
(`current_agent`/`agent_db_session`), never a human session. They stay on tenant_api
exactly as they are; an admin acting on a tenant's behalf never impersonates a device.

Also excludes the `diagnostic.read` / support-grant nuance the original's `/diagnostics`
route carries (a separate, narrower, time-limited support-access mechanism) — everything
here gates uniformly on `tenant.infra.manage`, the one bundled permission this whole admin
surface shares.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import get_settings
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.outbound import (
    BlockedAddressError,
    parse_networks,
    validate_allowlist_candidate,
)
from csense_shared.security.permissions import require_permission
from csense_shared.security.signed_commands import sign_command
from csense_shared.security.tenant_context import PlatformContext
from csense_shared.security.vpn_pool import next_free_address

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/tenants/{tenant_id}/edge", tags=["admin-tenant-edge"])

DEVICE_TYPES = (
    "raspberry_pi", "jetson_nano", "jetson_orin", "dgx_spark",
    "pc_linux", "pc_windows", "other",
)
DEVICE_ROLES = ("gateway", "inference", "hybrid")
HEALTH_LEVELS = ("infrastructure", "service", "quality")
OFFLINE_AFTER = dt.timedelta(minutes=5)
ENROLMENT_TOKEN_TTL = dt.timedelta(hours=48)
TOKEN_PREFIX_LENGTH = 8
DEFAULT_COMMAND_TTL_SECONDS = 3600
MAX_COMMAND_TTL_SECONDS = 7 * 24 * 3600
CONFIG_PUSH_COMMAND_TYPE = "config_push"
CONFIG_PUSH_FIELD_NAMES = (
    "heartbeat_interval_seconds", "spool_max_rows", "spool_max_bytes",
    "backoff_initial_seconds", "backoff_max_seconds",
)


def _generate_token() -> tuple[str, str, str]:
    plaintext = secrets.token_urlsafe(32)
    return (
        plaintext,
        plaintext[:TOKEN_PREFIX_LENGTH],
        hashlib.sha256(plaintext.encode()).hexdigest(),
    )


def _json(value: dict, limit: int = 8192) -> str:
    import json

    encoded = json.dumps(value)
    if len(encoded) > limit:
        raise ApiError(status_code=422, code="payload_too_large", message=f"Payload exceeds {limit // 1024} KB.")
    return encoded


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    site_id: uuid.UUID | None = None
    device_type: str = Field(default="other", pattern="^(" + "|".join(DEVICE_TYPES) + ")$")
    role: str = Field(default="gateway", pattern="^(" + "|".join(DEVICE_ROLES) + ")$")
    serial_number: str | None = Field(default=None, max_length=120)


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    site_id: uuid.UUID | None = None
    device_type: str | None = Field(default=None, pattern="^(" + "|".join(DEVICE_TYPES) + ")$")
    role: str | None = Field(default=None, pattern="^(" + "|".join(DEVICE_ROLES) + ")$")
    status: str | None = Field(default=None, pattern="^(disabled|retired|enrolled)$")


class DeviceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    site_id: uuid.UUID | None = None
    site_name: str | None = None
    name: str
    serial_number: str | None = None
    device_type: str
    role: str
    status: str
    online: bool
    hardware: dict = {}
    capabilities: dict = {}
    health: dict = {}
    health_status: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    agent_version: str | None = None
    connectivity_method: str | None = None
    connectivity_reason: str | None = None
    vpn_address: str | None = None
    lan_cidr: str | None = None
    has_wireguard_key: bool = False
    camera_count: int = 0
    enrolled_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    last_error: str | None = None
    created_at: dt.datetime
    desired_state_version: int = 0
    observed_state_version: int = 0


class EnrolmentTokenOut(BaseModel):
    device_id: uuid.UUID
    token: str
    expires_at: dt.datetime
    instructions: str


_SELECT = """
    SELECT d.id, d.tenant_id, d.site_id, s.name, d.name, d.serial_number, d.device_type,
           d.role, d.status, d.hardware, d.capabilities, d.os_name, d.os_version,
           d.agent_version, d.connectivity_method, d.connectivity_reason,
           host(d.vpn_address), text(d.lan_cidr), (d.wireguard_public_key IS NOT NULL),
           d.enrolled_at, d.last_seen_at, d.last_error, d.created_at,
           (SELECT count(*) FROM cameras c
             WHERE c.edge_device_id = d.id AND c.deleted_at IS NULL),
           d.health_status, d.health,
           d.desired_state_version, d.observed_state_version
    FROM edge_devices d
    LEFT JOIN sites s ON s.id = d.site_id
"""


def _to_device(row, now: dt.datetime) -> DeviceOut:
    last_seen = row[20]
    return DeviceOut(
        id=row[0], tenant_id=row[1], site_id=row[2], site_name=row[3], name=row[4],
        serial_number=row[5], device_type=row[6], role=row[7], status=row[8],
        online=bool(last_seen and (now - last_seen) < OFFLINE_AFTER),
        hardware=row[9] or {}, capabilities=row[10] or {}, os_name=row[11],
        os_version=row[12], agent_version=row[13], connectivity_method=row[14],
        connectivity_reason=row[15], vpn_address=row[16], lan_cidr=row[17],
        has_wireguard_key=bool(row[18]), enrolled_at=row[19],
        last_seen_at=last_seen, last_error=row[21], created_at=row[22],
        camera_count=row[23], health_status=row[24], health=row[25] or {},
        desired_state_version=row[26], observed_state_version=row[27],
    )


async def _load(db: AsyncSession, tenant_id: uuid.UUID, device_id: uuid.UUID) -> DeviceOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE d.id = :id AND d.tenant_id = :tenant_id AND d.deleted_at IS NULL"),
            {"id": device_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such edge device.")
    return _to_device(row, dt.datetime.now(dt.UTC))


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    tenant_id: uuid.UUID,
    site_id: uuid.UUID | None = None,
    role: str | None = Query(default=None, pattern="^(" + "|".join(DEVICE_ROLES) + ")$"),
    limit: int = Query(default=50, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[DeviceOut]:
    require_permission(context, "tenant.infra.manage")
    clauses = ["d.tenant_id = :tenant_id", "d.deleted_at IS NULL"]
    params: dict = {"tenant_id": tenant_id, "limit": limit}
    if site_id:
        clauses.append("d.site_id = :site_id")
        params["site_id"] = site_id
    if role:
        clauses.append("d.role = :role")
        params["role"] = role

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY d.name LIMIT :limit"),
            params,
        )
    ).all()
    now = dt.datetime.now(dt.UTC)
    return [_to_device(row, now) for row in rows]


@router.get("/devices/{device_id}", response_model=DeviceOut)
async def get_device(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> DeviceOut:
    require_permission(context, "tenant.infra.manage")
    return await _load(db, tenant_id, device_id)


@router.post("/devices", response_model=DeviceOut, status_code=201)
async def create_device(
    tenant_id: uuid.UUID,
    body: DeviceIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> DeviceOut:
    require_permission(context, "tenant.infra.manage")

    if body.site_id is not None:
        site = (
            await db.execute(
                text("SELECT 1 FROM sites WHERE id = :id AND tenant_id = :tenant_id"),
                {"id": body.site_id, "tenant_id": tenant_id},
            )
        ).first()
        if site is None:
            raise NotFoundError("No such site.")

    if body.serial_number:
        # Global, not tenant-scoped, matching the tenant-facing original: a serial
        # identifies one physical device platform-wide, not one per tenant.
        clash = (
            await db.execute(
                text("SELECT 1 FROM edge_devices WHERE serial_number = :s AND deleted_at IS NULL"),
                {"s": body.serial_number},
            )
        ).first()
        if clash:
            raise ApiError(
                status_code=409,
                code="serial_already_registered",
                message=(
                    f"Serial '{body.serial_number}' is already registered. A serial "
                    "identifies one physical device; two records claiming it means a "
                    "duplicate registration or a cloned device."
                ),
            )

    device_id = (
        await db.execute(
            text(
                """
                INSERT INTO edge_devices
                    (tenant_id, site_id, name, device_type, role, serial_number, status)
                VALUES (:tenant_id, :site_id, :name, :device_type, :role, :serial, 'pending')
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id, "site_id": body.site_id, "name": body.name,
                "device_type": body.device_type, "role": body.role, "serial": body.serial_number,
            },
        )
    ).scalar_one()

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.create",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        after_patch={"name": body.name, "role": body.role},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_edge_device_created", extra={"device_id": str(device_id), "tenant_id": str(tenant_id)})
    return await _load(db, tenant_id, device_id)


@router.patch("/devices/{device_id}", response_model=DeviceOut)
async def update_device(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    body: DevicePatch,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> DeviceOut:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, device_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await _load(db, tenant_id, device_id)

    if "site_id" in changes and changes["site_id"] is not None:
        site = (
            await db.execute(
                text("SELECT 1 FROM sites WHERE id = :id AND tenant_id = :tenant_id"),
                {"id": changes["site_id"], "tenant_id": tenant_id},
            )
        ).first()
        if site is None:
            raise NotFoundError("No such site.")

    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    await db.execute(
        text(
            f"UPDATE edge_devices SET {assignments}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
        ),
        {"id": device_id, "tenant_id": tenant_id, **changes},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.update",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        after_patch=changes,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_edge_device_updated", extra={"device_id": str(device_id), "fields": sorted(changes)})
    return await _load(db, tenant_id, device_id)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> None:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, device_id)

    await db.execute(
        text(
            """
            UPDATE edge_devices
            SET deleted_at = now(), status = 'retired',
                agent_token_hash = NULL, agent_token_prefix = NULL,
                vpn_address = NULL, wireguard_public_key = NULL, lan_cidr = NULL,
                updated_at = now()
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {"id": device_id, "tenant_id": tenant_id},
    )
    await db.execute(
        text("DELETE FROM edge_enrolment_tokens WHERE device_id = :id AND redeemed_at IS NULL"),
        {"id": device_id},
    )
    await db.execute(
        text("UPDATE cameras SET edge_device_id = NULL WHERE edge_device_id = :id AND tenant_id = :tenant_id"),
        {"id": device_id, "tenant_id": tenant_id},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.retire",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info("admin_edge_device_retired", extra={"device_id": str(device_id)})


@router.post("/devices/{device_id}/enrolment-token", response_model=EnrolmentTokenOut, status_code=201)
async def issue_enrolment_token(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> EnrolmentTokenOut:
    require_permission(context, "tenant.infra.manage")
    device = await _load(db, tenant_id, device_id)

    if device.status in ("disabled", "retired"):
        raise ApiError(
            status_code=409,
            code="device_not_enrollable",
            message=f"This device is {device.status}. Re-enable it before enrolling.",
        )

    plaintext, prefix, digest = _generate_token()
    expires_at = dt.datetime.now(dt.UTC) + ENROLMENT_TOKEN_TTL

    await db.execute(
        text("DELETE FROM edge_enrolment_tokens WHERE device_id = :id AND redeemed_at IS NULL"),
        {"id": device_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO edge_enrolment_tokens
                (tenant_id, device_id, token_hash, token_prefix, expires_at, created_by)
            VALUES (:tenant_id, :device_id, :hash, :prefix, :expires_at, :created_by)
            """
        ),
        {
            "tenant_id": tenant_id, "device_id": device_id, "hash": digest,
            "prefix": prefix, "expires_at": expires_at, "created_by": context.developer_user_id,
        },
    )
    logger.info(
        "admin_edge_enrolment_token_issued",
        extra={"device_id": str(device_id), "token_prefix": prefix, "actor": str(context.developer_user_id)},
    )
    return EnrolmentTokenOut(
        device_id=device_id,
        token=plaintext,
        expires_at=expires_at,
        instructions=(
            "Copy this token onto the device's deployment media now - it is not shown "
            "again. It can be redeemed once, and expires in 48 hours."
        ),
    )


def _safe_comment(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch not in "\r\n[]").strip()
    return cleaned[:120] or "edge device"


def _render_server_peer_block(*, name: str, public_key: str, vpn_address: str, lan_cidr: str | None) -> str:
    allowed = f"{vpn_address}/32" if not lan_cidr else f"{vpn_address}/32, {lan_cidr}"
    return f"# {_safe_comment(name)}\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = {allowed}\n"


def _render_client_config(*, vpn_address: str, settings) -> str:
    lines = [
        "[Interface]",
        "# Paste the private key generated on this device - it never leaves the device,",
        "# and CSense neither sees nor stores it.",
        "PrivateKey = <paste your private key here>",
        f"Address = {vpn_address}/32",
        "",
        "[Peer]",
    ]
    if settings.wireguard_server_public_key:
        lines.append(f"PublicKey = {settings.wireguard_server_public_key}")
    else:
        lines.append("# PublicKey = <not configured - set WIREGUARD_SERVER_PUBLIC_KEY>")
    if settings.wireguard_server_endpoint:
        lines.append(f"Endpoint = {settings.wireguard_server_endpoint}")
    else:
        lines.append("# Endpoint = <not configured - set WIREGUARD_SERVER_ENDPOINT>")
    if settings.wireguard_server_address:
        lines.append(f"AllowedIPs = {settings.wireguard_server_address}")
    else:
        lines.append("# AllowedIPs = <not configured - set WIREGUARD_SERVER_ADDRESS>")
    lines.append("PersistentKeepalive = 25")
    return "\n".join(lines)


class VpnProvisionIn(BaseModel):
    lan_cidr: str | None = Field(default=None, max_length=64)


class VpnProvisionOut(BaseModel):
    device: DeviceOut
    server_peer_config: str
    client_config: str
    warnings: list[str] = []


@router.post("/devices/{device_id}/vpn-provision", response_model=VpnProvisionOut)
async def provision_vpn(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    body: VpnProvisionIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> VpnProvisionOut:
    require_permission(context, "tenant.infra.manage")

    row = (
        await db.execute(
            text(
                "SELECT wireguard_public_key, host(vpn_address), text(lan_cidr), name, status "
                "FROM edge_devices WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"
            ),
            {"id": device_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such edge device.")
    public_key, existing_address, existing_lan, name, status = row

    if not public_key:
        raise ApiError(
            status_code=409,
            code="no_wireguard_key",
            message=(
                "This device has not reported a WireGuard public key yet. It needs to "
                "enrol with one before a tunnel can be provisioned."
            ),
        )
    if status in ("disabled", "retired"):
        raise ApiError(status_code=409, code="device_not_provisionable", message=f"This device is {status}.")

    settings = get_settings()
    reserved = parse_networks(settings.reserved_local_networks.split(","))

    lan_cidr: str | None = None
    if body.lan_cidr and body.lan_cidr.strip():
        try:
            lan_cidr = str(validate_allowlist_candidate(body.lan_cidr, reserved=reserved))
        except BlockedAddressError as exc:
            raise ApiError(status_code=422, code="lan_cidr_rejected", message=str(exc)) from exc

    # Same fleet-wide table lock and cross-tenant collision check as the tenant-facing
    # original - this is still the one shared WireGuard server for every tenant, and this
    # check needs to see every tenant's allocation regardless of who is calling it.
    await db.execute(text("LOCK TABLE edge_devices IN SHARE ROW EXCLUSIVE MODE"))

    fleet_vpn_addresses: set[str] = set()
    fleet_lan_cidrs: list[str] = []
    for vpn_addr, lan in (
        await db.execute(text("SELECT host(vpn_address), text(lan_cidr) FROM edge_vpn_pool_snapshot()"))
    ).all():
        if vpn_addr:
            fleet_vpn_addresses.add(vpn_addr)
        if lan:
            fleet_lan_cidrs.append(lan)

    if lan_cidr:
        candidate_net = ipaddress.ip_network(lan_cidr, strict=False)
        for other_cidr in fleet_lan_cidrs:
            if other_cidr == existing_lan:
                continue
            if candidate_net.overlaps(ipaddress.ip_network(other_cidr, strict=False)):
                raise ApiError(
                    status_code=409,
                    code="lan_cidr_overlap",
                    message=(
                        f"{lan_cidr} overlaps a network another device already tunnels. "
                        "Declare a narrower range that covers just the cameras, or "
                        "renumber the site LAN."
                    ),
                )

    vpn_address = existing_address
    if vpn_address is None:
        pool = ipaddress.ip_network(settings.wireguard_pool_cidr)
        allocated = next_free_address(pool, fleet_vpn_addresses)
        if allocated is None:
            raise ApiError(status_code=409, code="vpn_pool_exhausted", message="Every address in the tunnel pool is already allocated.")
        vpn_address = str(allocated)

    await db.execute(
        text(
            "UPDATE edge_devices SET vpn_address = :addr, lan_cidr = :lan, "
            "updated_at = now(), version = version + 1 WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"addr": vpn_address, "lan": lan_cidr, "id": device_id, "tenant_id": tenant_id},
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.vpn_provision",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        after_patch={"vpn_address": vpn_address, "has_lan_cidr": bool(lan_cidr)},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    logger.info(
        "admin_edge_device_vpn_provisioned",
        extra={"device_id": str(device_id), "vpn_address": vpn_address, "has_lan_cidr": bool(lan_cidr)},
    )

    warnings = []
    if not settings.wireguard_server_public_key or not settings.wireguard_server_endpoint:
        warnings.append(
            "The platform's own WireGuard server identity is not configured "
            "(WIREGUARD_SERVER_PUBLIC_KEY / WIREGUARD_SERVER_ENDPOINT), so the client "
            "config below is incomplete."
        )

    return VpnProvisionOut(
        device=await _load(db, tenant_id, device_id),
        server_peer_config=_render_server_peer_block(
            name=name, public_key=public_key, vpn_address=vpn_address, lan_cidr=lan_cidr,
        ),
        client_config=_render_client_config(vpn_address=vpn_address, settings=settings),
        warnings=warnings,
    )


class HealthEventOut(BaseModel):
    level: str
    check_name: str
    status: str
    detail: str | None = None
    metrics: dict = {}
    observed_at: dt.datetime
    received_at: dt.datetime


@router.get("/devices/{device_id}/health", response_model=list[HealthEventOut])
async def device_health_history(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    level: str | None = Query(default=None, pattern="^(" + "|".join(HEALTH_LEVELS) + ")$"),
    limit: int = Query(default=50, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[HealthEventOut]:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, device_id)

    clauses = ["device_id = :device_id", "tenant_id = :tenant_id"]
    params: dict = {"device_id": device_id, "tenant_id": tenant_id, "limit": limit}
    if level:
        clauses.append("level = :level")
        params["level"] = level

    rows = (
        await db.execute(
            text(
                "SELECT level, check_name, status, detail, metrics, observed_at, received_at "
                f"FROM edge_health_events WHERE {' AND '.join(clauses)} "
                "ORDER BY observed_at DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    return [
        HealthEventOut(level=r[0], check_name=r[1], status=r[2], detail=r[3], metrics=r[4] or {}, observed_at=r[5], received_at=r[6])
        for r in rows
    ]


class IssueCommandIn(BaseModel):
    command_type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=DEFAULT_COMMAND_TTL_SECONDS, ge=30, le=MAX_COMMAND_TTL_SECONDS)


class CommandOut(BaseModel):
    id: str
    edge_device_id: str
    command_type: str
    payload: dict
    idempotency_key: str
    status: str
    not_before: dt.datetime
    expires_at: dt.datetime
    issued_at: dt.datetime
    delivered_at: dt.datetime | None
    completed_at: dt.datetime | None
    result_code: str | None
    result_summary: str | None
    signed_envelope: str


_COMMAND_SELECT = (
    "SELECT id, edge_device_id, command_type, payload, idempotency_key, status::text, "
    "not_before, expires_at, issued_at, delivered_at, completed_at, result_code, "
    "result_summary, signed_envelope FROM device_commands"
)


def _command_out(row) -> CommandOut:
    return CommandOut(
        id=str(row[0]), edge_device_id=str(row[1]), command_type=row[2], payload=row[3] or {},
        idempotency_key=row[4], status=row[5], not_before=row[6], expires_at=row[7],
        issued_at=row[8], delivered_at=row[9], completed_at=row[10], result_code=row[11],
        result_summary=row[12], signed_envelope=row[13],
    )


@router.post("/devices/{device_id}/commands", response_model=CommandOut, status_code=201)
async def issue_command(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    body: IssueCommandIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> CommandOut:
    require_permission(context, "tenant.infra.manage")
    if body.command_type == CONFIG_PUSH_COMMAND_TYPE:
        raise ApiError(
            status_code=422,
            code="use_config_push_route",
            message=(
                f"'{CONFIG_PUSH_COMMAND_TYPE}' commands must be issued through "
                f"POST /devices/{{id}}/config, not this generic route."
            ),
        )
    await _load(db, tenant_id, device_id)

    existing = (
        await db.execute(
            text(f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id AND idempotency_key = :key"),
            {"device_id": device_id, "key": body.idempotency_key},
        )
    ).first()
    if existing is not None:
        return _command_out(existing)

    settings = get_settings()
    command_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(seconds=body.ttl_seconds)

    signed_envelope = sign_command(
        settings=settings, command_id=command_id, edge_device_id=device_id,
        command_type=body.command_type, payload=body.payload, idempotency_key=body.idempotency_key,
        not_before=now.timestamp(), expires_at=expires_at.timestamp(),
    )
    await db.execute(
        text(
            "INSERT INTO device_commands "
            "(id, tenant_id, edge_device_id, command_type, payload, idempotency_key, "
            " not_before, expires_at, issued_by, signed_envelope) "
            "VALUES (:id, :tenant_id, :device_id, :command_type, CAST(:payload AS jsonb), "
            " :key, :not_before, :expires_at, :issued_by, :envelope)"
        ),
        {
            "id": command_id, "tenant_id": tenant_id, "device_id": device_id,
            "command_type": body.command_type, "payload": _json(body.payload),
            "key": body.idempotency_key, "not_before": now, "expires_at": expires_at,
            "issued_by": context.developer_user_id, "envelope": signed_envelope,
        },
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.command.issue",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        reason=f"Issued {body.command_type}",
        after_patch={"command_id": str(command_id), "command_type": body.command_type},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    result = (await db.execute(text(f"{_COMMAND_SELECT} WHERE id = :id"), {"id": command_id})).first()
    return _command_out(result)


class ConfigPushIn(BaseModel):
    heartbeat_interval_seconds: int | None = Field(default=None, ge=1)
    spool_max_rows: int | None = Field(default=None, ge=1)
    spool_max_bytes: int | None = Field(default=None, ge=1)
    backoff_initial_seconds: float | None = Field(default=None, gt=0)
    backoff_max_seconds: float | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=DEFAULT_COMMAND_TTL_SECONDS, ge=30, le=MAX_COMMAND_TTL_SECONDS)


class ConfigPushOut(BaseModel):
    device: DeviceOut
    command: CommandOut


@router.post("/devices/{device_id}/config", response_model=ConfigPushOut, status_code=201)
async def push_config(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    body: ConfigPushIn,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> ConfigPushOut:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, device_id)

    fields = body.model_dump(exclude={"idempotency_key", "ttl_seconds"}, exclude_none=True)
    if not fields:
        raise ApiError(
            status_code=422, code="empty_config_push",
            message="A config push must set at least one of: " + ", ".join(CONFIG_PUSH_FIELD_NAMES),
        )

    existing = (
        await db.execute(
            text(f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id AND idempotency_key = :key"),
            {"device_id": device_id, "key": body.idempotency_key},
        )
    ).first()
    if existing is not None:
        return ConfigPushOut(device=await _load(db, tenant_id, device_id), command=_command_out(existing))

    new_version = (
        await db.execute(
            text(
                "UPDATE edge_devices SET desired_state_version = desired_state_version + 1, "
                "updated_at = now() WHERE id = :id AND tenant_id = :tenant_id RETURNING desired_state_version"
            ),
            {"id": device_id, "tenant_id": tenant_id},
        )
    ).scalar_one()

    settings = get_settings()
    command_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(seconds=body.ttl_seconds)
    payload = {**fields, "target_version": new_version}

    signed_envelope = sign_command(
        settings=settings, command_id=command_id, edge_device_id=device_id,
        command_type=CONFIG_PUSH_COMMAND_TYPE, payload=payload,
        idempotency_key=body.idempotency_key,
        not_before=now.timestamp(), expires_at=expires_at.timestamp(),
    )
    await db.execute(
        text(
            "INSERT INTO device_commands "
            "(id, tenant_id, edge_device_id, command_type, payload, idempotency_key, "
            " not_before, expires_at, issued_by, signed_envelope) "
            "VALUES (:id, :tenant_id, :device_id, :command_type, CAST(:payload AS jsonb), "
            " :key, :not_before, :expires_at, :issued_by, :envelope)"
        ),
        {
            "id": command_id, "tenant_id": tenant_id, "device_id": device_id,
            "command_type": CONFIG_PUSH_COMMAND_TYPE, "payload": _json(payload),
            "key": body.idempotency_key, "not_before": now, "expires_at": expires_at,
            "issued_by": context.developer_user_id, "envelope": signed_envelope,
        },
    )
    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="tenant_edge_device.command.config_push",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        reason=f"Pushed config, target_version={new_version}",
        after_patch={"command_id": str(command_id), "target_version": new_version, "fields": sorted(fields)},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
    )
    result = (await db.execute(text(f"{_COMMAND_SELECT} WHERE id = :id"), {"id": command_id})).first()
    return ConfigPushOut(device=await _load(db, tenant_id, device_id), command=_command_out(result))


@router.get("/devices/{device_id}/commands", response_model=list[CommandOut])
async def list_commands(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> list[CommandOut]:
    require_permission(context, "tenant.infra.manage")
    await _load(db, tenant_id, device_id)

    rows = (
        await db.execute(
            text(f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id ORDER BY issued_at DESC LIMIT :limit"),
            {"device_id": device_id, "limit": limit},
        )
    ).all()
    return [_command_out(r) for r in rows]


class DiagnosticCommandOut(BaseModel):
    command_type: str
    status: str
    result_code: str | None
    result_summary: str | None
    issued_at: dt.datetime
    delivered_at: dt.datetime | None
    completed_at: dt.datetime | None


def _diagnostic_command_out(row) -> DiagnosticCommandOut:
    return DiagnosticCommandOut(
        command_type=row[2], status=row[5], result_code=row[11], result_summary=row[12],
        issued_at=row[8], delivered_at=row[9], completed_at=row[10],
    )


class DiagnosticsOut(BaseModel):
    device_id: uuid.UUID
    health: dict
    health_status: str | None = None
    connectivity_method: str | None = None
    connectivity_reason: str | None = None
    last_seen_at: dt.datetime | None = None
    commands: list[DiagnosticCommandOut]


DIAGNOSTICS_COMMAND_LIMIT = 10


@router.get("/devices/{device_id}/diagnostics", response_model=DiagnosticsOut)
async def device_diagnostics(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> DiagnosticsOut:
    require_permission(context, "tenant.infra.manage")
    device = await _load(db, tenant_id, device_id)

    rows = (
        await db.execute(
            text(
                f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id ORDER BY issued_at DESC LIMIT :limit"
            ),
            {"device_id": device_id, "limit": DIAGNOSTICS_COMMAND_LIMIT},
        )
    ).all()
    return DiagnosticsOut(
        device_id=device.id,
        health=device.health,
        health_status=device.health_status,
        connectivity_method=device.connectivity_method,
        connectivity_reason=device.connectivity_reason,
        last_seen_at=device.last_seen_at,
        commands=[_diagnostic_command_out(r) for r in rows],
    )
