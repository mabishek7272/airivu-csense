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
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant
from app.deps_agent import AgentContext, agent_db_session, current_agent
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

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
    os_name: str | None = None
    os_version: str | None = None
    agent_version: str | None = None
    connectivity_method: str | None = None
    connectivity_reason: str | None = None
    vpn_address: str | None = None
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
           host(d.vpn_address), d.enrolled_at, d.last_seen_at, d.last_error, d.created_at,
           (SELECT count(*) FROM cameras c
             WHERE c.edge_device_id = d.id AND c.deleted_at IS NULL)
    FROM edge_devices d
    LEFT JOIN sites s ON s.id = d.site_id
"""


def _to_device(row, now: dt.datetime) -> DeviceOut:
    last_seen = row[18]
    return DeviceOut(
        id=row[0], tenant_id=row[1], site_id=row[2], site_name=row[3], name=row[4],
        serial_number=row[5], device_type=row[6], role=row[7], status=row[8],
        online=bool(last_seen and (now - last_seen) < OFFLINE_AFTER),
        hardware=row[9] or {}, capabilities=row[10] or {}, os_name=row[11],
        os_version=row[12], agent_version=row[13], connectivity_method=row[14],
        connectivity_reason=row[15], vpn_address=row[16], enrolled_at=row[17],
        last_seen_at=last_seen, last_error=row[19], created_at=row[20],
        camera_count=row[21],
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
                vpn_address = NULL, wireguard_public_key = NULL, updated_at = now()
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
    wireguard_public_key: str | None = Field(default=None, max_length=120)


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
            "health": _json(body.health, limit=16384),
            "health_status": body.status,
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

    if body.status != "ok" or recorded:
        # Only worth a log line when something is wrong or changed; an "everything fine"
        # line every 30 seconds per device drowns out the ones that matter.
        logger.info(
            "edge_heartbeat",
            extra={
                "device_id": agent.device_id,
                "health_status": body.status,
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
