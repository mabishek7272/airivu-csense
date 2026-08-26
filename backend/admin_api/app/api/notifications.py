"""Platform notification-provider administration (TRD §18, §21).

Two jobs:

  **WhatsApp instance lifecycle.** Connecting a number means scanning a QR code, and that
  QR is short-lived, so the console needs create/qr/status/logout rather than a single
  "configure" form. Instance state is platform-level, not tenant-level: one gateway number
  serves many tenants, and only a platform operator should be able to re-link it.

  **Open-source attribution.** Evolution Go's licence (§1.b) requires a notice visible to
  system administrators that it is in use. `/components` is that notice, served from the
  API so it cannot drift out of sync with what is actually deployed.

The gateway itself is never exposed publicly: its API key grants the ability to send as
your WhatsApp number. Everything here proxies through the Admin API so the key stays
inside the Docker network and every action is permission-checked and audited.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_platform_context, platform_db_session
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.errors import ApiError
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import PlatformContext

router = APIRouter(prefix="/api/v1/admin/notifications", tags=["admin-notifications"])


class ProviderStatus(BaseModel):
    channel: str
    provider_code: str
    configured: bool
    detail: str | None = None


class InstanceStatus(BaseModel):
    instance: str
    connected: bool
    state: str
    detail: str | None = None


class CreateInstanceRequest(BaseModel):
    instance: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class LicenceStatus(BaseModel):
    """Activation state of the WhatsApp gateway's own vendor licence."""

    activated: bool
    state: str
    instance_id: str | None = None
    register_url: str | None = None
    guidance: str | None = None


class QrResponse(BaseModel):
    instance: str
    # Base64 PNG or the raw pairing string, depending on what the gateway returns.
    qr_code: str | None
    pairing_code: str | None = None
    expires_in_seconds: int = 60


class OpenSourceComponent(BaseModel):
    name: str
    version: str | None
    licence: str
    used_for: str
    project_url: str
    notice: str | None = None


def _gateway(request: Request):
    """The configured WhatsApp provider, or a clear error if none is set up."""
    provider = request.app.state.provider_registry.get("whatsapp")
    if provider is None or not hasattr(provider, "instance_status"):
        raise ApiError(
            status_code=503,
            code="whatsapp_not_configured",
            message=(
                "No WhatsApp gateway is configured. Set WHATSAPP_GATEWAY_URL and "
                "WHATSAPP_GATEWAY_API_KEY, then restart the Admin API."
            ),
        )
    return provider


@router.get("/providers", response_model=list[ProviderStatus])
async def list_providers(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
) -> list[ProviderStatus]:
    """Which channels can actually send right now.

    An unconfigured channel is reported rather than hidden: "why did nobody get the
    alert" is answered here, not by reading environment variables on a server.
    """
    require_permission(context, "notification.read")
    registry = request.app.state.provider_registry
    configured = registry.describe()

    return [
        ProviderStatus(
            channel=channel,
            provider_code=configured.get(channel, "none"),
            configured=channel in configured,
            detail=None if channel in configured else "No provider configured for this channel.",
        )
        for channel in ("email", "whatsapp", "sms", "web_push", "in_app", "webhook")
    ]


@router.get("/whatsapp/licence", response_model=LicenceStatus)
async def whatsapp_licence(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
) -> LicenceStatus:
    """Licence activation state, and how to activate if it is not.

    Evolution Go gates every endpoint behind activation with Evolution Foundation. Until
    it is done, WhatsApp alerting cannot work at all - and without this endpoint that
    looks like an unexplained outage rather than a one-time setup step somebody owes.
    """
    require_permission(context, "notification.read")
    provider = _gateway(request)

    status = await provider.licence_status()
    if status.get("activated"):
        return LicenceStatus(
            activated=True, state=status.get("state", "active"),
            instance_id=status.get("instance_id"),
        )

    # Not activated: fetch the vendor's registration URL so an operator can complete it.
    registration = await provider.licence_registration_url()
    return LicenceStatus(
        activated=False,
        state=status.get("state", "inactive"),
        instance_id=status.get("instance_id"),
        register_url=registration.get("register_url"),
        guidance=(
            "The WhatsApp gateway is not activated. It is vendored from Evolution Go, "
            "which requires a licence to be registered with Evolution Foundation before "
            "any endpoint responds. Open the registration URL to complete activation, or "
            "set EVOLUTION_OPERATOR_EMAIL on the whatsapp-gateway service to an already "
            "registered address for headless activation. WhatsApp alerts will not send "
            "until this is done; email alerting is unaffected."
        ),
    )


@router.get("/whatsapp/status", response_model=InstanceStatus)
async def whatsapp_status(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
) -> InstanceStatus:
    require_permission(context, "notification.read")
    provider = _gateway(request)
    status = await provider.instance_status()
    return InstanceStatus(
        instance=status.get("instance", "default"),
        connected=bool(status.get("connected")),
        state=status.get("state", "unknown"),
        detail=status.get("detail"),
    )


@router.post("/whatsapp/instances", response_model=InstanceStatus, status_code=201)
async def create_whatsapp_instance(
    body: CreateInstanceRequest,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> InstanceStatus:
    """Creates a gateway instance. Linking a number is a separate QR step."""
    require_permission(context, "notification.manage")
    provider = _gateway(request)

    # The instance token comes from configuration and is supplied at creation, so it
    # never has to be captured from the response and stored.
    token = request.app.state.settings.whatsapp_instance_token
    if not token:
        raise ApiError(
            status_code=503,
            code="whatsapp_instance_token_missing",
            message=(
                "WHATSAPP_INSTANCE_TOKEN is not set. Generate one "
                "(openssl rand -hex 24), set it in .env, and restart the Admin API."
            ),
        )
    result = await provider.create_instance(body.instance, token)
    if not result.get("ok"):
        raise ApiError(
            status_code=502,
            code="gateway_error",
            message=result.get("detail") or "The WhatsApp gateway rejected the request.",
        )

    correlation_id = getattr(request.state, "correlation_id", None)
    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="notification.whatsapp.instance.created",
        outcome="success",
        target_type="whatsapp_instance",
        target_id=body.instance,
        correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
        event_type="notification.provider.changed.v1",
        event_payload={"channel": "whatsapp", "instance": body.instance, "action": "created"},
        aggregate_type="whatsapp_instance",
        aggregate_id=body.instance,
    )

    return InstanceStatus(
        instance=body.instance, connected=False, state="created",
        detail="Instance created. Fetch a QR code to link a WhatsApp number.",
    )


@router.get("/whatsapp/qr", response_model=QrResponse)
async def whatsapp_qr(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
) -> QrResponse:
    """Short-lived QR for linking a number.

    Deliberately not audited as a privileged mutation: it is a read, it is polled every
    few seconds by the console while someone scans, and auditing every poll would bury
    the actual link event in noise. The resulting connection *is* audited.
    """
    require_permission(context, "notification.manage")
    provider = _gateway(request)

    result = await provider.instance_qr()
    if not result.get("ok"):
        raise ApiError(
            status_code=502,
            code="gateway_error",
            message=result.get("detail") or "Could not obtain a QR code from the gateway.",
        )
    return QrResponse(
        instance=result.get("instance", "default"),
        qr_code=result.get("qr_code"),
        pairing_code=result.get("pairing_code"),
    )


@router.post("/whatsapp/logout", response_model=InstanceStatus)
async def whatsapp_logout(
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
) -> InstanceStatus:
    """Unlinks the WhatsApp number.

    Audited, because it stops WhatsApp alerting for every tenant on this gateway - the
    kind of action someone needs to be able to attribute afterwards.
    """
    require_permission(context, "notification.manage")
    provider = _gateway(request)
    result = await provider.instance_logout()

    correlation_id = getattr(request.state, "correlation_id", None)
    await record_audit_and_outbox(
        db,
        tenant_id=None,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="notification.whatsapp.instance.logout",
        outcome="success" if result.get("ok") else "failed",
        target_type="whatsapp_instance",
        target_id=result.get("instance", "default"),
        reason="WhatsApp number unlinked; WhatsApp alerts will stop until re-linked.",
        correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
        event_type="notification.provider.changed.v1",
        event_payload={"channel": "whatsapp", "action": "logout"},
        aggregate_type="whatsapp_instance",
        aggregate_id=result.get("instance", "default"),
    )

    return InstanceStatus(
        instance=result.get("instance", "default"),
        connected=False,
        state="logged_out",
        detail=result.get("detail"),
    )


@router.get("/components", response_model=list[OpenSourceComponent])
async def open_source_components(
    context: PlatformContext = Depends(current_platform_context),
) -> list[OpenSourceComponent]:
    """Third-party components in use, with their licences.

    Required, not decorative: Evolution Go's licence §1.b obliges us to display a clear
    notice to system administrators that it is being used, reachable from the system's
    settings. Served from the API so it reflects what is deployed rather than a wiki page
    that goes stale.
    """
    require_permission(context, "notification.read")
    return [
        OpenSourceComponent(
            name="Evolution Go",
            version=None,
            licence="Apache-2.0 with additional conditions",
            used_for="WhatsApp messaging gateway",
            project_url="https://github.com/EvolutionAPI/evolution-go",
            notice=(
                "This system uses Evolution Go by Evolution Foundation for WhatsApp "
                "messaging. Evolution Go is licensed under Apache 2.0 with additional "
                "conditions. Its console is not distributed with this system; CSense "
                "provides its own interface. Licensing enquiries: "
                "suporte@evofoundation.com.br"
            ),
        ),
        OpenSourceComponent(
            name="whatsmeow",
            version=None,
            licence="MPL-2.0",
            used_for="WhatsApp protocol implementation, via Evolution Go",
            project_url="https://github.com/tulir/whatsmeow",
        ),
        OpenSourceComponent(
            name="Ultralytics YOLOv8",
            version=None,
            licence="AGPL-3.0",
            used_for="Object detection and pose estimation models",
            project_url="https://github.com/ultralytics/ultralytics",
            notice=(
                "Five migrated detection models are Ultralytics YOLOv8 weights under "
                "AGPL-3.0. See CLARIFICATIONS #15."
            ),
        ),
        OpenSourceComponent(
            name="InsightFace",
            version=None,
            licence="Non-commercial research licence",
            used_for="Face detection and recognition models (quarantined)",
            project_url="https://github.com/deepinsight/insightface",
            notice=(
                "Biometric models are registered but classified 'biometric'. See "
                "CLARIFICATIONS #16."
            ),
        ),
        OpenSourceComponent(
            name="Resend",
            version=None,
            licence="Commercial service",
            used_for="Transactional email delivery",
            project_url="https://resend.com",
        ),
    ]
