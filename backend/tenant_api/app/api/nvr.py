"""NVR channel discovery, and the honest ONVIF-discovery stub.

Two genuinely different capabilities living in one file because FLOW-05 (camera discovery
and onboarding) treats them as the two halves of one flow, not because they share an
implementation - see `nvr_adapter.py` and `csense_shared.onvif.discovery` for why they are
architecturally different (one is a direct, central, unicast call; the other structurally
cannot run anywhere but the edge, which does not exist yet).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from app.services.camera_probe import device_tunnel_networks
from app.services.nvr_adapter import NVRChannel, build_adapter
from csense_shared.config import Settings
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.outbound import BlockedAddressError, resolve_public_endpoint
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant", tags=["nvr"])


class NvrDiscoverIn(BaseModel):
    hostname: str = Field(min_length=1, max_length=253)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)
    # Naming a device the tenant already enrolled is how a discovery request against a
    # private/VPN address gets validated - never "private addresses are fine" by default,
    # matching camera_probe.py's own stance.
    edge_device_id: uuid.UUID | None = None


class NvrChannelOut(BaseModel):
    channel_id: str
    name: str
    main_stream_path: str
    sub_stream_path: str | None
    vendor: str | None
    model: str | None


class NvrDiscoverOut(BaseModel):
    channels: list[NvrChannelOut]


def _to_out(channel: NVRChannel) -> NvrChannelOut:
    return NvrChannelOut(
        channel_id=channel.channel_id, name=channel.name,
        main_stream_path=channel.main_stream_path, sub_stream_path=channel.sub_stream_path,
        vendor=channel.vendor, model=channel.model,
    )


@router.post("/nvr/discover", response_model=NvrDiscoverOut)
async def discover_nvr_channels(
    body: NvrDiscoverIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> NvrDiscoverOut:
    """Lists an NVR's channels through the adapter interface (`nvr_adapter.py`) - the mock
    adapter this pass ships never actually connects, but the address is still validated as
    if a real one might, so swapping one in later inherits this boundary automatically
    rather than needing it added after the fact.

    Nothing here is persisted. Turning a returned channel into a real camera is a
    separate, existing call (`POST /cameras`) the caller makes with whatever channel it
    picked - this endpoint only ever looks.
    """
    require_permission(context, "camera.discover")

    allowed: list = []
    if body.edge_device_id is not None:
        owned = (
            await db.execute(
                text("SELECT 1 FROM edge_devices WHERE id = :id AND deleted_at IS NULL"),
                {"id": body.edge_device_id},
            )
        ).first()
        if owned is None:
            # RLS already scopes this - "not found" covers both "no such device" and
            # "not yours", indistinguishably, same as everywhere else in this API.
            raise NotFoundError("No such edge device.")
        allowed = await device_tunnel_networks(db, device_id=body.edge_device_id)

    try:
        resolve_public_endpoint(body.hostname, body.port, allowed_networks=allowed)
    except BlockedAddressError as exc:
        raise ApiError(status_code=422, code="address_not_permitted", message=str(exc)) from exc

    adapter = build_adapter("mock")
    channels = await adapter.list_channels(
        hostname=body.hostname, port=body.port, username=body.username, password=body.password,
    )
    return NvrDiscoverOut(channels=[_to_out(c) for c in channels])


class SiteDiscoverOut(BaseModel):
    available: bool
    reason: str
    cameras: list[dict] = []


@router.post("/sites/{site_id}/discover-cameras", response_model=SiteDiscoverOut)
async def discover_cameras_for_site(
    site_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SiteDiscoverOut:
    """The ONVIF-discovery half of FLOW-05, honestly stubbed.

    Real WS-Discovery is UDP multicast, scoped to one network segment - it cannot reach a
    customer's LAN from this cloud service, only from a device actually on that LAN
    (`csense_shared.onvif.discovery`'s own docstring goes into why). There is no edge
    agent yet (`edge/agent/` is empty) and no command-push channel to one either, so there
    is currently no way to actually perform or receive real ONVIF discovery results from
    anywhere. This says so plainly - `available: false` with a real reason - rather than
    returning an empty list that reads as "no cameras found" (a materially different,
    misleading claim) or fake results that look like a real scan.
    """
    require_permission(context, "camera.discover")

    site = (await db.execute(text("SELECT 1 FROM sites WHERE id = :id"), {"id": site_id})).first()
    if site is None:
        raise NotFoundError("No such site.")

    return SiteDiscoverOut(
        available=False,
        reason=(
            "ONVIF discovery requires an edge agent running on this site's own network - "
            "not yet deployed. Use 'Discover from NVR' with a known NVR address instead, "
            "or add a camera manually."
        ),
    )
