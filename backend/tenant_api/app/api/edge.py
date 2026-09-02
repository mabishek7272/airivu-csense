"""Edge — the devices CSense runs on at customer sites.

An edge device is whatever sits at the customer end: a Raspberry Pi carrying only
connectivity, a Jetson or DGX Spark running models locally, or an ordinary Windows or
Linux PC. They are not interchangeable, and the field that captures the difference is
`role`:

  `gateway`   — makes cameras reachable and nothing more. The cloud pulls the stream and
                runs the models, so this device costs the server roughly a core per camera.
  `inference` — runs the models itself and sends up detections rather than video. Costs
                the server almost nothing, and is the only way a 16-core box serves a
                large fleet.
  `hybrid`    — both.

Two credentials, deliberately separate:

  The **enrolment token** is single-use and short-lived. It travels - written to a USB
  stick, read out over the phone to an installer - and exists only to get a box from
  "unboxed" to "has an identity". It is shown once, at creation, and never again.

  The **agent credential** is issued at the end of enrolment and never leaves the device.
  It is what every heartbeat and every pushed detection authenticates with.

Reusing the enrolment token as the ongoing credential would make whatever path it
travelled by a permanent way in, which is why they are not the same value.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from app.deps_agent import AgentContext, agent_db_session, current_agent
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
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.vpn_pool import next_free_address

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant/edge", tags=["edge"])

DEVICE_TYPES = (
    "raspberry_pi", "jetson_nano", "jetson_orin", "dgx_spark",
    "pc_linux", "pc_windows", "other",
)
DEVICE_ROLES = ("gateway", "inference", "hybrid")

# The ACDK agent's three reporting tiers.
HEALTH_LEVELS = ("infrastructure", "service", "quality")
EVENT_STATUSES = ("ok", "degraded", "failed", "recovered")

# Health events are operational telemetry, not evidence: worth keeping long enough to
# investigate an outage, and no longer.
HEALTH_EVENT_RETENTION = dt.timedelta(days=30)

# Returned to the agent on every heartbeat rather than compiled into it, so the cadence
# can be widened during an incident without shipping firmware.
HEARTBEAT_INTERVAL_SECONDS = 30

# Long enough that an installer will not lose it, short enough that a token left on a
# desk is not still live weeks later.
ENROLMENT_TOKEN_TTL = dt.timedelta(hours=48)
TOKEN_PREFIX_LENGTH = 8

# A device is called offline once it has missed several heartbeats. Reported rather than
# stored, so the answer is always current even if nothing has written to the row.
OFFLINE_AFTER = dt.timedelta(minutes=5)


def _generate_token() -> tuple[str, str, str]:
    """Returns (plaintext, prefix, digest).

    256 bits from a CSPRNG. The digest is a plain SHA-256 rather than Argon2 on purpose:
    Argon2 exists to make guessing low-entropy secrets expensive, and there is nothing to
    guess here - a slow hash would only add latency to every enrolment attempt, which is
    itself a denial-of-service lever.
    """
    plaintext = secrets.token_urlsafe(32)
    return (
        plaintext,
        plaintext[:TOKEN_PREFIX_LENGTH],
        hashlib.sha256(plaintext.encode()).hexdigest(),
    )


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
    # Derived from the last heartbeat rather than stored, so it cannot go stale.
    online: bool
    hardware: dict = {}
    capabilities: dict = {}
    # The latest health snapshot and the summarised verdict drawn from it. Both were
    # written by every heartbeat and returned by nothing, so the spool telemetry the UI is
    # documented as showing ("spool use", docs/04_UI_UX_DESIGN_BRIEF.md) had no way of
    # reaching it, and neither did the degradation rule further down this file.
    # `/devices/{id}/health` is the *history* of transitions, not current state.
    health: dict = {}
    health_status: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    agent_version: str | None = None
    connectivity_method: str | None = None
    connectivity_reason: str | None = None
    vpn_address: str | None = None
    lan_cidr: str | None = None
    # Whether a tunnel *can* be provisioned - separate from whether one has been, because
    # the key only arrives at enrolment and there is no way to infer its presence from
    # anything else on this model.
    has_wireguard_key: bool = False
    camera_count: int = 0
    enrolled_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    last_error: str | None = None
    created_at: dt.datetime


class EnrolmentTokenOut(BaseModel):
    """The one and only time the plaintext token is returned."""

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
           -- Appended rather than slotted in beside `d.health` on purpose: every column
           -- here is read back by ordinal, and renumbering twenty-odd of them to add one
           -- is how an off-by-one gets shipped between two same-typed fields.
           d.health_status, d.health
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
    )


async def _load(db: AsyncSession, device_id: uuid.UUID) -> DeviceOut:
    row = (
        await db.execute(
            text(_SELECT + " WHERE d.id = :id AND d.deleted_at IS NULL"), {"id": device_id}
        )
    ).first()
    if row is None:
        raise NotFoundError("No such edge device.")
    return _to_device(row, dt.datetime.now(dt.UTC))


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    site_id: uuid.UUID | None = None,
    role: str | None = Query(default=None, pattern="^(" + "|".join(DEVICE_ROLES) + ")$"),
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[DeviceOut]:
    require_permission(context, "edge.read")

    clauses = ["d.deleted_at IS NULL"]
    params: dict = {"limit": limit}
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
    device_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DeviceOut:
    require_permission(context, "edge.read")
    return await _load(db, device_id)


@router.post("/devices", response_model=DeviceOut, status_code=201)
async def create_device(
    body: DeviceIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DeviceOut:
    """Registers a device record. It has no identity until it enrols."""
    require_permission(context, "edge.manage")

    if body.site_id is not None:
        site = (
            await db.execute(text("SELECT 1 FROM sites WHERE id = :id"), {"id": body.site_id})
        ).first()
        if site is None:
            # Row-level security already scopes this, so "not found" and "not yours" must
            # look identical or the API becomes a way to enumerate other tenants' sites.
            raise NotFoundError("No such site.")

    if body.serial_number:
        clash = (
            await db.execute(
                text(
                    "SELECT 1 FROM edge_devices WHERE serial_number = :s "
                    "AND deleted_at IS NULL"
                ),
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
                "tenant_id": context.tenant_id, "site_id": body.site_id, "name": body.name,
                "device_type": body.device_type, "role": body.role,
                "serial": body.serial_number,
            },
        )
    ).scalar_one()

    logger.info(
        "edge_device_created",
        extra={"device_id": str(device_id), "type": body.device_type, "role": body.role},
    )
    return await _load(db, device_id)


@router.patch("/devices/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> DeviceOut:
    require_permission(context, "edge.manage")
    await _load(db, device_id)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await _load(db, device_id)

    if "site_id" in changes and changes["site_id"] is not None:
        site = (
            await db.execute(
                text("SELECT 1 FROM sites WHERE id = :id"), {"id": changes["site_id"]}
            )
        ).first()
        if site is None:
            raise NotFoundError("No such site.")

    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    await db.execute(
        text(
            f"UPDATE edge_devices SET {assignments}, updated_at = now(), "
            "version = version + 1 WHERE id = :id AND deleted_at IS NULL"
        ),
        {"id": device_id, **changes},
    )
    logger.info(
        "edge_device_updated",
        extra={"device_id": str(device_id), "fields": sorted(changes)},
    )
    return await _load(db, device_id)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    """Retires a device and revokes everything it could authenticate with.

    Soft-deletes the record because cameras and detections reference it, but the agent
    credential and any unredeemed enrolment token are destroyed outright. A retired device
    that can still push detections is a device nobody is watching but everybody trusts.
    """
    require_permission(context, "edge.manage")
    await _load(db, device_id)

    await db.execute(
        text(
            """
            UPDATE edge_devices
            SET deleted_at = now(), status = 'retired',
                agent_token_hash = NULL, agent_token_prefix = NULL,
                vpn_address = NULL, wireguard_public_key = NULL, lan_cidr = NULL,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": device_id},
    )
    await db.execute(
        text("DELETE FROM edge_enrolment_tokens WHERE device_id = :id AND redeemed_at IS NULL"),
        {"id": device_id},
    )
    # Cameras stay, but stop pointing at a device that no longer exists.
    await db.execute(
        text("UPDATE cameras SET edge_device_id = NULL WHERE edge_device_id = :id"),
        {"id": device_id},
    )
    logger.info("edge_device_retired", extra={"device_id": str(device_id)})


@router.post(
    "/devices/{device_id}/enrolment-token",
    response_model=EnrolmentTokenOut,
    status_code=201,
)
async def issue_enrolment_token(
    device_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> EnrolmentTokenOut:
    """Issues a single-use token the device redeems to claim its identity.

    The plaintext is returned exactly once and is not recoverable afterwards. Issuing a
    new token invalidates any previous unredeemed one, so a token handed to a courier and
    then reissued cannot still be used.

    Behind `edge.enrol` rather than `edge.manage`: adding a device record is paperwork,
    minting the credential that makes an unconfigured box trusted is not.
    """
    require_permission(context, "edge.enrol")
    device = await _load(db, device_id)

    if device.status in ("disabled", "retired"):
        raise ApiError(
            status_code=409,
            code="device_not_enrollable",
            message=f"This device is {device.status}. Re-enable it before enrolling.",
        )

    plaintext, prefix, digest = _generate_token()
    expires_at = dt.datetime.now(dt.UTC) + ENROLMENT_TOKEN_TTL

    # Superseding any outstanding token is the point: two live tokens for one device means
    # two ways to claim it, and only one of them is being tracked by whoever asked.
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
            "tenant_id": context.tenant_id, "device_id": device_id, "hash": digest,
            "prefix": prefix, "expires_at": expires_at, "created_by": context.user_id,
        },
    )

    logger.info(
        "edge_enrolment_token_issued",
        extra={
            "device_id": str(device_id),
            "token_prefix": prefix,   # non-secret, so a log line can be matched to a token
            "actor": str(context.user_id),
        },
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


# --- WireGuard provisioning -------------------------------------------------------------
#
# The deployment guide's own templates have two flaws: every client hardcodes
# `10.0.0.2`, so the second site collides with the first, and every peer's
# `AllowedIPs = 10.0.0.0/24` lets one tenant's device route to another tenant's cameras -
# the /24 is shared by every peer on the one WireGuard server this platform runs, and
# nothing about it keeps two tenants' traffic apart. This is the fix for both: the address
# comes from a managed pool (vpn_pool.py) instead of being hand-picked, and the config this
# renders is the only place a peer stanza is meant to come from - it is never anything
# wider than that one device's own /32.


def _safe_comment(value: str) -> str:
    """Makes a tenant-controlled string safe to embed as a comment in a config an operator
    pastes into the platform's one *shared* WireGuard server.

    A device name is bounded in length elsewhere but not in character set, and this text
    is not read back by us - it is meant to be copied verbatim into a real file. A newline
    would end the comment early; a `[` could open what reads as a second, forged config
    section. Stripping exactly those is enough to guarantee the rendered block is always
    one comment line followed by one peer stanza, whatever a device is named.
    """
    cleaned = "".join(ch for ch in value if ch not in "\r\n[]").strip()
    return cleaned[:120] or "edge device"


def _render_server_peer_block(
    *, name: str, public_key: str, vpn_address: str, lan_cidr: str | None
) -> str:
    """The stanza an operator pastes into the shared server's `wg0.conf`.

    `AllowedIPs` is deliberately just this device's own address, plus its own site LAN if
    it has one - never the pool, never another device's range. That is what makes one
    tenant's peer unable to route to another's cameras: WireGuard enforces AllowedIPs as
    both the accepted source and the routed destination for a peer, so a peer whose
    AllowedIPs names only its own /32 (and its own LAN) has nothing else it *can* reach.
    """
    allowed = f"{vpn_address}/32" if not lan_cidr else f"{vpn_address}/32, {lan_cidr}"
    return (
        f"# {_safe_comment(name)}\n"
        "[Peer]\n"
        f"PublicKey = {public_key}\n"
        f"AllowedIPs = {allowed}\n"
    )


def _render_client_config(*, vpn_address: str, settings) -> str:
    """What goes on the device. It never learns the pool or any other peer's address -
    only its own address and the one peer it is allowed to talk to: the server."""
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
    lan_cidr: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The site network this device's tunnel routes, e.g. 192.168.1.0/24. Omit if "
            "the only address reachable through this device is its own."
        ),
    )


class VpnProvisionOut(BaseModel):
    device: DeviceOut
    server_peer_config: str
    client_config: str
    warnings: list[str] = []


@router.post("/devices/{device_id}/vpn-provision", response_model=VpnProvisionOut)
async def provision_vpn(
    device_id: uuid.UUID,
    body: VpnProvisionIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> VpnProvisionOut:
    """Allocates this device's tunnel address and renders the config for it.

    Exists so nobody hand-edits the deployment guide's templates again: this is now the
    only path that produces a peer entry, and it cannot produce the two mistakes that made
    the guide's own version unsafe - a colliding address or an over-broad `AllowedIPs`.

    Idempotent on the address: calling this again for a device that already has one
    re-renders its config rather than allocating a second address, so re-running it to
    change `lan_cidr` (or just to fetch the config text again) is always safe.
    """
    require_permission(context, "edge.manage")

    row = (
        await db.execute(
            text(
                "SELECT wireguard_public_key, host(vpn_address), text(lan_cidr), name, status "
                "FROM edge_devices WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": device_id},
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
                "enrol with one - generated on the device, never sent to us as anything "
                "but the public half - before a tunnel can be provisioned."
            ),
        )
    if status in ("disabled", "retired"):
        raise ApiError(
            status_code=409,
            code="device_not_provisionable",
            message=f"This device is {status}.",
        )

    settings = get_settings()
    reserved = parse_networks(settings.reserved_local_networks.split(","))

    lan_cidr: str | None = None
    if body.lan_cidr and body.lan_cidr.strip():
        try:
            lan_cidr = str(validate_allowlist_candidate(body.lan_cidr, reserved=reserved))
        except BlockedAddressError as exc:
            raise ApiError(
                status_code=422, code="lan_cidr_rejected", message=str(exc)
            ) from exc

    # This is an admin action taken once per device, not a hot path - locking the table
    # for the moment it takes to check for a collision and write the result is what makes
    # "no two peers ever end up with an overlapping route" true, rather than merely
    # unlikely under concurrent provisioning. The lock is table-level, not row-level, so
    # it serializes across tenants too - which matters, since the checks below deliberately
    # need to see every tenant's allocation, not just this one's.
    await db.execute(text("LOCK TABLE edge_devices IN SHARE ROW EXCLUSIVE MODE"))

    # Row-level security scopes an ordinary query to the calling tenant, which is right
    # everywhere else and wrong here: a collision with *another* tenant's device is
    # exactly what these two checks exist to catch, on the one WireGuard server the whole
    # fleet shares. `edge_vpn_pool_snapshot` (migration 0029) is a narrow SECURITY DEFINER
    # read built for exactly this - it returns addresses and ranges, never who they
    # belong to.
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
        # This device's own current lan_cidr (if any) is in the snapshot too - comparing
        # a range against itself would always "overlap" and block re-provisioning with
        # the same value.
        for other_cidr in fleet_lan_cidrs:
            if other_cidr == existing_lan:
                continue
            if candidate_net.overlaps(ipaddress.ip_network(other_cidr, strict=False)):
                raise ApiError(
                    status_code=409,
                    code="lan_cidr_overlap",
                    message=(
                        f"{lan_cidr} overlaps a network another device already tunnels. "
                        "Two peers on the same WireGuard server cannot share an "
                        "overlapping route - the second one configured would silently "
                        "take traffic meant for the first. Declare a narrower range that "
                        "covers just the cameras, or renumber the site LAN."
                    ),
                )

    vpn_address = existing_address
    if vpn_address is None:
        pool = ipaddress.ip_network(settings.wireguard_pool_cidr)
        allocated = next_free_address(pool, fleet_vpn_addresses)
        if allocated is None:
            raise ApiError(
                status_code=409,
                code="vpn_pool_exhausted",
                message="Every address in the tunnel pool is already allocated.",
            )
        vpn_address = str(allocated)

    await db.execute(
        text(
            "UPDATE edge_devices SET vpn_address = :addr, lan_cidr = :lan, "
            "updated_at = now(), version = version + 1 WHERE id = :id"
        ),
        {"addr": vpn_address, "lan": lan_cidr, "id": device_id},
    )

    logger.info(
        "edge_device_vpn_provisioned",
        extra={
            "device_id": str(device_id),
            "vpn_address": vpn_address,
            "has_lan_cidr": bool(lan_cidr),
        },
    )

    warnings = []
    if not settings.wireguard_server_public_key or not settings.wireguard_server_endpoint:
        warnings.append(
            "The platform's own WireGuard server identity is not configured "
            "(WIREGUARD_SERVER_PUBLIC_KEY / WIREGUARD_SERVER_ENDPOINT), so the client "
            "config below is incomplete."
        )

    return VpnProvisionOut(
        device=await _load(db, device_id),
        server_peer_config=_render_server_peer_block(
            name=name, public_key=public_key, vpn_address=vpn_address, lan_cidr=lan_cidr,
        ),
        client_config=_render_client_config(vpn_address=vpn_address, settings=settings),
        warnings=warnings,
    )


# --- Device-facing ---------------------------------------------------------------------

class EnrolIn(BaseModel):
    """What a device presents to claim its identity."""

    token: str = Field(min_length=16, max_length=200)
    # Pinned on first redemption, so a token copied off one box cannot be redeemed on
    # another.
    serial_number: str = Field(min_length=1, max_length=120)
    device_type: str | None = Field(default=None, pattern="^(" + "|".join(DEVICE_TYPES) + ")$")
    hardware: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    os_name: str | None = Field(default=None, max_length=80)
    os_version: str | None = Field(default=None, max_length=80)
    agent_version: str | None = Field(default=None, max_length=40)
    # A Curve25519 public key, base64-encoded: exactly 44 characters. Enforced here, not
    # just checked for length, because this value is later embedded verbatim into a
    # server-side config an operator pastes into the platform's one shared WireGuard
    # server - a value that did not have to look like a key could smuggle a newline and a
    # second, forged `[Peer]` stanza into what looks like a single line.
    wireguard_public_key: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9+/]{43}=$"
    )


class EnrolOut(BaseModel):
    device_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    role: str
    # The long-lived credential. Returned once; only its digest is stored.
    agent_token: str


@router.post("/enrol", response_model=EnrolOut, status_code=201)
async def enrol(
    body: EnrolIn,
    request: Request,
) -> EnrolOut:
    """Redeems an enrolment token and issues the device its lasting credential.

    Deliberately outside the tenant JWT scheme: the caller is a freshly-imaged box that has
    no user session and no tenant context. The token *is* the authentication, which is why
    it is single-use, expiring, and pinned to a serial on redemption.

    The tenant is derived from the token, never from the request - a device does not get to
    say which tenant it belongs to.
    """
    presented = body.token.strip()
    prefix = presented[:TOKEN_PREFIX_LENGTH]
    digest = hashlib.sha256(presented.encode()).hexdigest()

    factory = request.app.state.session_factory
    async with factory() as db, db.begin():
        # The lookup runs through a narrow SECURITY DEFINER function (migration 0025).
        # Row-level security scopes queries by app.tenant_id, and a device presenting a
        # token does not yet know its tenant - discovering it is the point of enrolling.
        # The Tenant API's role deliberately cannot bypass RLS, and that must stay true,
        # so the exception is one function that takes a prefix and returns one row.
        row = (
            await db.execute(
                text("SELECT * FROM edge_enrolment_lookup(:prefix)"), {"prefix": prefix}
            )
        ).first()

        # One message for every failure below. Distinguishing "no such token" from
        # "expired" from "already used" would let someone probe which tokens exist.
        rejection = ApiError(
            status_code=401,
            code="enrolment_rejected",
            message="That enrolment token is not valid, has expired, or has been used.",
        )

        if row is None:
            logger.warning("edge_enrolment_unknown_token", extra={"token_prefix": prefix})
            raise rejection

        # Constant-time: the digest is compared, not the token, and never with ==.
        if not hmac.compare_digest(row[3], digest):
            logger.warning("edge_enrolment_bad_token", extra={"token_prefix": prefix})
            raise rejection
        if row[5] is not None:
            logger.warning(
                "edge_enrolment_token_reused",
                extra={"token_prefix": prefix, "device_id": str(row[1])},
            )
            raise rejection
        if row[4] <= dt.datetime.now(dt.UTC):
            logger.info("edge_enrolment_token_expired", extra={"token_prefix": prefix})
            raise rejection
        if row[9] in ("disabled", "retired"):
            raise rejection

        # If the record already names a serial, the device presenting a different one is
        # not the device the token was issued for.
        registered_serial = row[8]
        if registered_serial and registered_serial != body.serial_number:
            logger.warning(
                "edge_enrolment_serial_mismatch",
                extra={"device_id": str(row[1]), "token_prefix": prefix},
            )
            raise rejection

        agent_token, agent_prefix, agent_digest = _generate_token()
        now = dt.datetime.now(dt.UTC)

        # The token has now established which tenant this device belongs to, so everything
        # below runs under normal row-level security rather than any elevated privilege.
        # Scoped to the transaction, so it cannot leak to the next use of this connection.
        await db.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(row[2])}
        )

        await db.execute(
            text(
                """
                UPDATE edge_devices
                SET status = 'enrolled',
                    serial_number = :serial,
                    device_type = COALESCE(:device_type, device_type),
                    hardware = CAST(:hardware AS jsonb),
                    capabilities = CAST(:capabilities AS jsonb),
                    os_name = :os_name, os_version = :os_version,
                    agent_version = :agent_version,
                    wireguard_public_key = COALESCE(:wg_key, wireguard_public_key),
                    agent_token_hash = :agent_hash,
                    agent_token_prefix = :agent_prefix,
                    agent_token_issued_at = :now,
                    enrolled_at = COALESCE(enrolled_at, :now),
                    last_seen_at = :now,
                    last_error = NULL,
                    updated_at = :now, version = version + 1
                WHERE id = :id
                """
            ),
            {
                "id": row[1], "serial": body.serial_number, "device_type": body.device_type,
                "hardware": _json(body.hardware), "capabilities": _json(body.capabilities),
                "os_name": body.os_name, "os_version": body.os_version,
                "agent_version": body.agent_version, "wg_key": body.wireguard_public_key,
                "agent_hash": agent_digest, "agent_prefix": agent_prefix, "now": now,
            },
        )
        await db.execute(
            text(
                "UPDATE edge_enrolment_tokens SET redeemed_at = :now, "
                "redeemed_by_serial = :serial WHERE id = :id"
            ),
            {"id": row[0], "now": now, "serial": body.serial_number},
        )

    logger.info(
        "edge_device_enrolled",
        extra={
            "device_id": str(row[1]),
            "tenant_id": str(row[2]),
            "device_type": body.device_type,
            "agent_version": body.agent_version,
        },
    )
    return EnrolOut(
        device_id=row[1], tenant_id=row[2], name=row[6], role=row[7],
        agent_token=agent_token,
    )


def _json(value: dict, limit: int = 8192) -> str:
    import json

    # Bounded: these blobs come from a device, and an unbounded one would let a
    # compromised box fill the table.
    encoded = json.dumps(value)
    if len(encoded) > limit:
        raise ApiError(
            status_code=422,
            code="payload_too_large",
            message=f"Reported payload exceeds {limit // 1024} KB.",
        )
    return encoded


# --- Heartbeat -------------------------------------------------------------------------
#
# Spool telemetry (FLOW-13). A device that loses its uplink keeps running its pipeline and
# writes events into an encrypted local spool. Two numbers describe that spool:
# `spool_depth`, how many events are waiting in it right now, and `spool_dropped`, how many
# it has had to evict because it filled. Both are named as device telemetry the UI shows
# (docs/05_BACKEND_SCHEMA.md, docs/04_UI_UX_DESIGN_BRIEF.md's "spool use") and until now
# neither had anywhere to arrive.
#
# **They land in the existing `health` JSONB snapshot, not in new columns.** That is a
# decision, not a shortcut. `health` exists precisely because "a Jetson reporting GPU
# temperature and a Pi reporting tunnel handshake age have little in common" (migration
# 0026) - spool depth is one more per-device gauge of exactly that kind: overwritten in
# place every heartbeat, read one device at a time, never filtered or sorted across the
# fleet. Dedicated columns would buy an index nothing asks for and two NULLs on every
# `gateway`-role device, which has no spool at all. `health_status` stays the summarised,
# filterable field, and it is the one the degradation rule below moves.

# A spool counter past this is a broken agent rather than a busy site: these events carry
# frames, and no edge disk holds two billion of them. Bounded because the numbers come
# from a device and end up verbatim in a JSON document an operator reads.
MAX_SPOOL_COUNTER = 2**31 - 1


def _spool_snapshot(
    previous: dict | None, *, depth: int | None, dropped: int | None, now: dt.datetime
) -> tuple[dict, int]:
    """The `spool` entry for the health snapshot, and how many events this device has
    dropped *since its last heartbeat*.

    `spool_dropped` is a cumulative counter that lives with the spool file on the device -
    it has to be, or it would reset on every reboot, and a reboot is how most outages end.
    That makes the raw number useless as a health signal on its own: a box that evicted one
    event a fortnight ago reports the same `1` forever. The delta against the value stored
    at the previous heartbeat is the number that means "this device is losing events right
    now", and it is the only one the rule below reads.

    A counter that has gone *down* means the spool was recreated - a reimaged device, a
    replaced disk, a wiped data volume - so the whole of its current value is loss we have
    not seen before, rather than a negative delta to be clamped away.

    `last_dropped_at` is carried forward when nothing new was dropped. It is what lets an
    operator still see "this device did lose events, at 03:12 on Tuesday" after the status
    has correctly gone back to `ok` - the half of the story a self-clearing status would
    otherwise throw away.
    """
    previous = previous if isinstance(previous, dict) else {}
    prior_dropped = previous.get("dropped")
    if not isinstance(prior_dropped, int) or isinstance(prior_dropped, bool) or prior_dropped < 0:
        # Whatever else is in this free-form snapshot, a heartbeat has to land: a device
        # that cannot report is a device that looks offline.
        prior_dropped = None

    total = dropped if dropped is not None else (prior_dropped or 0)
    if prior_dropped is None:
        # No prior value: an agent reporting spool telemetry for the first time. Charging
        # its whole counter to "now" costs one heartbeat of `degraded` and no more, which
        # is the right way round - a box that has been silently evicting events should say
        # so once, loudly, rather than never.
        new_drops = total
    elif total < prior_dropped:
        new_drops = total
    else:
        new_drops = total - prior_dropped

    block: dict = {
        "depth": depth if depth is not None else previous.get("depth"),
        "dropped": total,
        "dropped_since_last_heartbeat": new_drops,
    }
    if new_drops > 0:
        block["last_dropped_at"] = now.isoformat()
    elif previous.get("last_dropped_at"):
        block["last_dropped_at"] = previous["last_dropped_at"]
    return block, new_drops


def _health_status_with_spool(reported: str, new_drops: int) -> str:
    """Whether the platform overrides a device's own verdict on itself because of the spool.

    A device that has dropped events since its last heartbeat is degraded whether it says
    so or not: those events are gone permanently, and the agent whose spool is overflowing
    is the one least likely to notice. So new loss escalates `ok` to `degraded`.

    Two things this deliberately does not do:

    **Depth alone is never degradation.** A deep spool is the spool working - a device
    offline for a day is *supposed* to have thousands of rows waiting - and treating that
    as a fault would flag every site whose broadband blinked.

    **It cannot latch.** The escalation is driven by the per-heartbeat delta, never by the
    cumulative counter, so a device that stops dropping is back to `ok` on its very next
    heartbeat with no flag for anything to clear. Nothing here persists a verdict that
    stale data could keep alive. The historical fact stays visible as `spool.dropped` and
    `spool.last_dropped_at` in the snapshot, which is where a fact belongs - not as a
    status that no longer describes the device.

    It never downgrades: a device reporting `failed` knows something we do not.
    """
    if new_drops > 0 and reported == "ok":
        return "degraded"
    return reported


class HealthCheckIn(BaseModel):
    """One check the agent ran.

    `observed_at` is when the *device* saw it, which is not when we received it. A device
    that was offline reports its cached events on reconnect, and the gap between those two
    timestamps is exactly what an outage investigation needs.
    """

    level: str = Field(pattern="^(" + "|".join(HEALTH_LEVELS) + ")$")
    check_name: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern="^(" + "|".join(EVENT_STATUSES) + ")$")
    detail: str | None = Field(default=None, max_length=1000)
    metrics: dict = Field(default_factory=dict)
    observed_at: dt.datetime | None = None


class HeartbeatIn(BaseModel):
    # Overall verdict, so a listing can be filtered without unpacking the snapshot.
    status: str = Field(default="ok", pattern="^(ok|degraded|failed)$")
    # Current state per tier, overwritten in place rather than appended.
    health: dict = Field(default_factory=dict)
    # Only transitions and failures. A device reporting every 30 seconds that everything
    # is fine should send an empty list, not 2,880 rows a day saying nothing happened.
    events: list[HealthCheckIn] = Field(default_factory=list, max_length=100)
    connectivity_method: str | None = Field(
        default=None, pattern="^(wireguard|cloud_relay|port_forward|direct)$"
    )
    connectivity_reason: str | None = Field(default=None, max_length=500)
    agent_version: str | None = Field(default=None, max_length=40)
    last_error: str | None = Field(default=None, max_length=1000)
    # The offline spool, per FLOW-13. Both optional, and they have to be: a fleet already
    # in the field predates the spool entirely, and a `gateway`-role device never has one.
    # A heartbeat is not the place to start demanding a field a deployed device cannot
    # send - the failure mode is the device looking offline, which is worse than the
    # telemetry being absent.
    spool_depth: int | None = Field(default=None, ge=0, le=MAX_SPOOL_COUNTER)
    spool_dropped: int | None = Field(default=None, ge=0, le=MAX_SPOOL_COUNTER)


class HeartbeatOut(BaseModel):
    acknowledged: bool
    events_recorded: int
    # How long the device should wait before reporting again. Returned rather than
    # hardcoded in the agent so the interval can be widened during an incident without
    # shipping firmware.
    next_interval_seconds: int


class HealthEventOut(BaseModel):
    level: str
    check_name: str
    status: str
    detail: str | None = None
    metrics: dict = {}
    observed_at: dt.datetime
    received_at: dt.datetime


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    body: HeartbeatIn,
    agent: AgentContext = Depends(current_agent),
    db: AsyncSession = Depends(agent_db_session),
) -> HeartbeatOut:
    """Records a device's current health, and any transitions it saw.

    Authenticated by the device's own credential - there is no user session here. The
    tenant comes from that credential, so a device cannot write against another tenant
    even if it tries.
    """
    now = dt.datetime.now(dt.UTC)
    device_id = uuid.UUID(agent.device_id)
    tenant_id = uuid.UUID(agent.tenant_id)

    health = dict(body.health)
    health_status = body.status
    if body.spool_depth is not None or body.spool_dropped is not None:
        # One extra read, and only for devices that actually report a spool, because the
        # drop *delta* cannot be computed without the previous heartbeat's counter. See
        # `_spool_snapshot` for why the delta rather than the counter is the health signal.
        previous_spool = (
            await db.execute(
                text("SELECT health -> 'spool' FROM edge_devices WHERE id = :id"),
                {"id": device_id},
            )
        ).scalar_one_or_none()
        health["spool"], new_drops = _spool_snapshot(
            previous_spool, depth=body.spool_depth, dropped=body.spool_dropped, now=now
        )
        health_status = _health_status_with_spool(body.status, new_drops)
        if new_drops:
            logger.warning(
                "edge_spool_dropped_events",
                extra={
                    "device_id": agent.device_id,
                    "dropped_since_last_heartbeat": new_drops,
                    "dropped_total": health["spool"]["dropped"],
                    "spool_depth": health["spool"]["depth"],
                },
            )

    await db.execute(
        text(
            """
            UPDATE edge_devices
            SET status = CASE WHEN status IN ('enrolled', 'offline') THEN 'online'
                              ELSE status END,
                health = CAST(:health AS jsonb),
                health_status = :health_status,
                connectivity_method = COALESCE(:method, connectivity_method),
                connectivity_reason = COALESCE(:reason, connectivity_reason),
                agent_version = COALESCE(:agent_version, agent_version),
                last_error = :last_error,
                last_seen_at = :now,
                last_health_at = :now,
                updated_at = :now
            WHERE id = :id
            """
        ),
        {
            "id": device_id,
            "health": _json(health, limit=16384),
            "health_status": health_status,
            "method": body.connectivity_method,
            "reason": body.connectivity_reason,
            "agent_version": body.agent_version,
            "last_error": body.last_error,
            "now": now,
        },
    )

    recorded = 0
    for event in body.events:
        observed = event.observed_at or now
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=dt.UTC)
        # A device with a wrong clock must not park an event in the future where it sorts
        # above everything real forever.
        observed = min(observed, now + dt.timedelta(minutes=5))

        await db.execute(
            text(
                """
                INSERT INTO edge_health_events
                    (tenant_id, device_id, level, check_name, status, detail, metrics,
                     observed_at, received_at, expires_at)
                VALUES (:tenant_id, :device_id, :level, :check_name, :status, :detail,
                        CAST(:metrics AS jsonb), :observed_at, :now, :expires_at)
                """
            ),
            {
                "tenant_id": tenant_id, "device_id": device_id, "level": event.level,
                "check_name": event.check_name, "status": event.status,
                "detail": event.detail, "metrics": _json(event.metrics, limit=4096),
                "observed_at": observed, "now": now,
                "expires_at": now + HEALTH_EVENT_RETENTION,
            },
        )
        recorded += 1

    if health_status != "ok" or recorded:
        # Only worth a log line when something is wrong or changed; an "everything fine"
        # line every 30 seconds per device drowns out the ones that matter. The escalated
        # status is what gets logged, not the device's own claim - the whole point of the
        # override is that the device's `ok` was not the last word.
        logger.info(
            "edge_heartbeat",
            extra={
                "device_id": agent.device_id,
                "health_status": health_status,
                "events": recorded,
            },
        )

    return HeartbeatOut(
        acknowledged=True,
        events_recorded=recorded,
        next_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )


@router.get("/devices/{device_id}/health", response_model=list[HealthEventOut])
async def device_health_history(
    device_id: uuid.UUID,
    level: str | None = Query(default=None, pattern="^(" + "|".join(HEALTH_LEVELS) + ")$"),
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[HealthEventOut]:
    """What actually happened to this device, most recent first."""
    require_permission(context, "edge.read")
    await _load(db, device_id)

    clauses = ["device_id = :device_id"]
    params: dict = {"device_id": device_id, "limit": limit}
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
        HealthEventOut(
            level=r[0], check_name=r[1], status=r[2], detail=r[3], metrics=r[4] or {},
            observed_at=r[5], received_at=r[6],
        )
        for r in rows
    ]


# --- Signed commands: expiry + idempotency, desired-state push to the device --------
#
# CHECKLIST: "Signed commands with expiry/idempotency (desired-state push to the
# device)". Issue/list below are operator-facing (`edge.manage`/`edge.read`, a person's
# own session); poll/ack are device-facing (`current_agent`, the device's own credential
# - the same authentication `/heartbeat` already uses). See
# `csense_shared.security.signed_commands`'s own docstring for what "signed" means here
# and why there is no consuming edge agent yet to poll/ack for real.

DEFAULT_COMMAND_TTL_SECONDS = 3600
MAX_COMMAND_TTL_SECONDS = 7 * 24 * 3600


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
    device_id: uuid.UUID,
    body: IssueCommandIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> CommandOut:
    """Issues a signed, expiring command to a device. Idempotent: reissuing with the
    same `idempotency_key` for the same device returns the command that already exists
    rather than creating a second one or erroring - a retried request and the request it
    was retrying must be indistinguishable in their effect.
    """
    require_permission(context, "edge.manage")
    await _load(db, device_id)  # 404s before doing anything if this isn't a real device

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
            "id": command_id, "tenant_id": context.tenant_id, "device_id": device_id,
            "command_type": body.command_type, "payload": _json(body.payload),
            "key": body.idempotency_key, "not_before": now, "expires_at": expires_at,
            "issued_by": context.user_id, "envelope": signed_envelope,
        },
    )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="edge.command.issue",
        outcome="success",
        target_type="edge_device",
        target_id=str(device_id),
        reason=f"Issued {body.command_type}",
        before_patch=None,
        after_patch={"command_id": str(command_id), "command_type": body.command_type},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="edge.command.issued.v1",
        event_payload={
            "command_id": str(command_id), "edge_device_id": str(device_id),
            "command_type": body.command_type,
        },
        aggregate_type="edge_device",
        aggregate_id=str(device_id),
    )

    result = (await db.execute(text(f"{_COMMAND_SELECT} WHERE id = :id"), {"id": command_id})).first()
    return _command_out(result)


@router.get("/devices/{device_id}/commands", response_model=list[CommandOut])
async def list_commands(
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[CommandOut]:
    require_permission(context, "edge.read")
    await _load(db, device_id)

    rows = (
        await db.execute(
            text(f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id ORDER BY issued_at DESC LIMIT :limit"),
            {"device_id": device_id, "limit": limit},
        )
    ).all()
    return [_command_out(r) for r in rows]


@router.get("/commands/pending", response_model=list[CommandOut])
async def poll_pending_commands(
    agent: AgentContext = Depends(current_agent),
    db: AsyncSession = Depends(agent_db_session),
) -> list[CommandOut]:
    """A device's own poll for its undelivered, currently-valid commands - authenticated
    by the device's own credential, the same as `/heartbeat`. Marks every command
    returned as `delivered` in the same call: a device that asks "what do you have for
    me" and gets an answer has, by definition, just received it.
    """
    device_id = uuid.UUID(agent.device_id)
    now = dt.datetime.now(dt.UTC)

    rows = (
        await db.execute(
            text(
                f"{_COMMAND_SELECT} WHERE edge_device_id = :device_id AND status = 'pending' "
                "AND not_before <= :now AND expires_at > :now ORDER BY issued_at"
            ),
            {"device_id": device_id, "now": now},
        )
    ).all()

    if rows:
        ids = [r[0] for r in rows]
        await db.execute(
            text("UPDATE device_commands SET status = 'delivered', delivered_at = :now WHERE id = ANY(:ids)"),
            {"now": now, "ids": ids},
        )

    return [_command_out(r) for r in rows]


class AckCommandIn(BaseModel):
    result_code: str = Field(min_length=1, max_length=64)
    result_summary: str | None = Field(default=None, max_length=2000)
    success: bool


@router.post("/commands/{command_id}/ack", response_model=CommandOut)
async def ack_command(
    command_id: uuid.UUID,
    body: AckCommandIn,
    agent: AgentContext = Depends(current_agent),
    db: AsyncSession = Depends(agent_db_session),
) -> CommandOut:
    """A device reports what happened when it tried to apply a command. Scoped to the
    calling device's own commands - `agent_db_session`'s tenant scope alone would still
    let a device ack another device's command within the same tenant, which it must not
    be able to do."""
    device_id = uuid.UUID(agent.device_id)
    existing = (
        await db.execute(
            text(f"{_COMMAND_SELECT} WHERE id = :id AND edge_device_id = :device_id"),
            {"id": command_id, "device_id": device_id},
        )
    ).first()
    if existing is None:
        raise NotFoundError("No such command for this device.")

    await db.execute(
        text(
            "UPDATE device_commands SET status = :status, completed_at = now(), "
            "result_code = :code, result_summary = :summary WHERE id = :id"
        ),
        {
            "status": "completed" if body.success else "failed",
            "code": body.result_code, "summary": body.result_summary, "id": command_id,
        },
    )

    result = (await db.execute(text(f"{_COMMAND_SELECT} WHERE id = :id"), {"id": command_id})).first()
    return _command_out(result)
