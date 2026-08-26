"""WhatsApp providers.

Two implementations behind one interface:

  `EvolutionGoWhatsAppProvider` - the vendored Go service (see edge/whatsapp-gateway).
      It speaks the WhatsApp Web protocol via whatsmeow, which Meta's terms prohibit.
      Numbers get banned, without warning and without a distinguishable error - the send
      simply starts failing. That is the whole reason this file has two classes.

  `CloudApiWhatsAppProvider` - Meta's official WhatsApp Business Cloud API. Not wired up
      yet (needs Business verification and template approval), but the shape is fixed here
      so switching is configuration rather than a rewrite (CLARIFICATIONS #22).

A ban is invisible at the API level, so `EvolutionGoWhatsAppProvider` classifies a
disconnected instance as a *permanent* failure with a distinct code. Marking it retryable
would bury a dead integration under a retry queue that never drains, which is exactly how
alerting stops working without anyone being told.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from csense_shared.notifications.providers import (
    Channel,
    DeliveryOutcome,
    Message,
    SendResult,
    redact,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Failure codes worth alerting an operator about, as opposed to a routine bounce.
INSTANCE_UNAVAILABLE = "whatsapp_instance_unavailable"


class EvolutionGoWhatsAppProvider:
    """Talks to the vendored WhatsApp gateway's HTTP API.

    One gateway instance is one connected WhatsApp number. The instance name identifies
    which number a tenant sends from; a deployment can run several.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("WhatsApp gateway base URL and API key are required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._instance = instance
        self._client = client

    @property
    def code(self) -> str:
        return "whatsapp-gateway"

    @property
    def channel(self) -> Channel:
        return Channel.WHATSAPP

    def validate_recipient(self, recipient: str) -> bool:
        # E.164 only. WhatsApp silently misroutes ambiguous local formats, which looks
        # like a delivered message that nobody ever received.
        return bool(E164.match(recipient))

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"apikey": self._api_key, "Content-Type": "application/json"}
        url = f"{self._base_url}{path}"
        if self._client is not None:
            return await self._client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    async def instance_status(self) -> dict[str, Any]:
        """Connection state of the underlying WhatsApp session.

        Surfaced in the Developer Console: a disconnected instance means alerts are not
        going out, and that must be visible before anyone needs it rather than discovered
        during an incident.
        """
        try:
            response = await self._request(
                "GET", "/instance/status", params={"instance": self._instance}
            )
        except httpx.HTTPError as exc:
            return {"connected": False, "state": "unreachable", "detail": redact(str(exc))}

        if response.status_code != 200:
            return {
                "connected": False,
                "state": "error",
                "detail": redact(response.text),
                "status_code": response.status_code,
            }

        body = self._safe_json(response)
        state = str(body.get("state") or body.get("status") or "unknown").lower()
        return {
            "connected": state in ("open", "connected", "online"),
            "state": state,
            "instance": self._instance,
        }

    async def licence_status(self) -> dict[str, Any]:
        """Whether the gateway's own licence is activated.

        Evolution Go gates **every** API endpoint behind licence activation with Evolution
        Foundation, returning 503 `LICENSE_REQUIRED` until it is done. Without this check
        an unactivated gateway looks identical to a broken one, and the actual cause -
        "you need to register with the vendor" - is invisible.

        Activation is a legitimate vendor requirement and is completed by an operator, not
        bypassed. See `licence_registration_url`.
        """
        try:
            # /license/status sits outside the licence gate, so it answers even when the
            # rest of the API does not.
            response = await self._request("GET", "/license/status")
        except httpx.HTTPError as exc:
            return {"activated": False, "state": "unreachable", "detail": redact(str(exc))}

        if response.status_code != 200:
            return {"activated": False, "state": "error", "detail": redact(response.text)}

        body = self._safe_json(response)
        return {
            "activated": body.get("status") == "active",
            "state": body.get("status", "unknown"),
            "instance_id": body.get("instance_id"),
        }

    async def licence_registration_url(self) -> dict[str, Any]:
        """Obtains the vendor's registration URL so an operator can activate the licence.

        This calls Evolution Foundation's service. It is the vendor's intended activation
        flow, surfaced in our console because we do not ship their manager UI.
        """
        try:
            response = await self._request("GET", "/license/register")
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": redact(str(exc))}

        if response.status_code != 200:
            return {"ok": False, "detail": redact(response.text)}

        body = self._safe_json(response)
        return {
            "ok": True,
            "status": body.get("status"),
            "register_url": body.get("register_url"),
            "message": body.get("message"),
        }

    async def create_instance(self, instance: str) -> dict[str, Any]:
        """Creates a gateway instance. Linking a number is a separate QR step."""
        try:
            response = await self._request("POST", "/instance/create", json={"instance": instance})
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": redact(str(exc))}

        if response.status_code in (200, 201):
            return {"ok": True, "instance": instance}
        # 409 means it already exists. Treated as success: creating is idempotent from the
        # console's point of view, and failing here would block the QR step for no reason.
        if response.status_code == 409:
            return {"ok": True, "instance": instance, "detail": "Instance already exists."}
        return {"ok": False, "detail": redact(response.text), "status_code": response.status_code}

    async def instance_qr(self) -> dict[str, Any]:
        """Fetches the current pairing QR.

        The QR rotates every ~20 seconds, so this is polled rather than cached. Nothing is
        persisted: a captured QR is enough to link a device, so it stays in flight only.
        """
        try:
            response = await self._request("GET", "/instance/qr", params={"instance": self._instance})
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": redact(str(exc))}

        if response.status_code != 200:
            return {"ok": False, "detail": redact(response.text), "status_code": response.status_code}

        body = self._safe_json(response)
        return {
            "ok": True,
            "instance": self._instance,
            "qr_code": body.get("qrcode") or body.get("qr") or body.get("base64"),
            "pairing_code": body.get("pairingCode") or body.get("code"),
        }

    async def instance_logout(self) -> dict[str, Any]:
        """Unlinks the WhatsApp number from this instance."""
        try:
            response = await self._request("POST", "/instance/logout", json={"instance": self._instance})
        except httpx.HTTPError as exc:
            return {"ok": False, "instance": self._instance, "detail": redact(str(exc))}

        ok = response.status_code in (200, 204)
        return {
            "ok": ok,
            "instance": self._instance,
            "detail": None if ok else redact(response.text),
        }

    async def send(self, message: Message) -> SendResult:
        if not self.validate_recipient(message.recipient):
            return SendResult(
                DeliveryOutcome.FAILED_PERMANENT,
                failure_code="invalid_recipient",
                failure_summary="Recipient is not a valid E.164 phone number.",
            )

        # Media send when there is a snapshot, plain text otherwise. An intrusion alert
        # with the annotated frame attached is actionable; the same words alone are not.
        if message.media_urls:
            path = "/send/media"
            payload: dict[str, Any] = {
                "instance": self._instance,
                "number": message.recipient.lstrip("+"),
                "mediatype": "image",
                "media": message.media_urls[0],
                "caption": message.body,
            }
        else:
            path = "/send/text"
            payload = {
                "instance": self._instance,
                "number": message.recipient.lstrip("+"),
                "text": message.body,
            }

        try:
            response = await self._request("POST", path, json=payload)
        except httpx.TimeoutException as exc:
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code="timeout",
                failure_summary=redact(str(exc)),
            )
        except httpx.HTTPError as exc:
            # The gateway itself is unreachable - a deployment problem, worth retrying.
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code="gateway_unreachable",
                failure_summary=redact(str(exc)),
            )

        return self._interpret(response)

    def _interpret(self, response: httpx.Response) -> SendResult:
        summary = redact(response.text)

        if response.status_code in (200, 201):
            body = self._safe_json(response)
            key = body.get("key") if isinstance(body, dict) else None
            message_id = key.get("id") if isinstance(key, dict) else body.get("id") if isinstance(body, dict) else None
            return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id=message_id)

        if response.status_code in (401, 403):
            return SendResult(
                DeliveryOutcome.FAILED_PERMANENT,
                failure_code="gateway_auth_rejected",
                failure_summary="WhatsApp gateway rejected the API key.",
            )

        # 404 on a send means the instance does not exist; 428/409 typically mean it is
        # not connected. Either way the session is gone - most often because the number
        # was banned or the linked device was removed. Permanent on purpose: this needs a
        # human to re-link, and retrying would hide that.
        if response.status_code in (404, 409, 428):
            logger.error(
                "whatsapp_instance_unavailable",
                extra={"instance": self._instance, "status": response.status_code},
            )
            return SendResult(
                DeliveryOutcome.FAILED_PERMANENT,
                failure_code=INSTANCE_UNAVAILABLE,
                failure_summary=(
                    "WhatsApp instance is not connected. The number may have been banned "
                    "or the linked device removed; it must be re-linked in the console."
                ),
            )

        if response.status_code == 429 or response.status_code >= 500:
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code=f"gateway_{response.status_code}",
                failure_summary=summary,
            )

        return SendResult(
            DeliveryOutcome.FAILED_PERMANENT,
            failure_code=f"rejected_{response.status_code}",
            failure_summary=summary,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}


class CloudApiWhatsAppProvider:
    """Meta's official WhatsApp Business Cloud API.

    Deliberately unimplemented rather than absent: it fixes the interface now, and makes
    the migration path visible in the codebase instead of living in a decision document.

    Two things differ materially from the unofficial gateway, and both need doing before
    this can send anything:

      - Business-initiated messages outside a 24-hour customer service window must use a
        **pre-approved template**. An alert is always business-initiated, so every alert
        type needs its own approved template - free-form text will be rejected.
      - Media must be uploaded to Meta first and referenced by id; a public URL is not
        accepted the way the gateway accepts one.
    """

    def __init__(self, phone_number_id: str, access_token: str, *, api_version: str = "v21.0") -> None:
        self._phone_number_id = phone_number_id
        self._access_token = access_token
        self._api_version = api_version

    @property
    def code(self) -> str:
        return "whatsapp-cloud-api"

    @property
    def channel(self) -> Channel:
        return Channel.WHATSAPP

    def validate_recipient(self, recipient: str) -> bool:
        return bool(E164.match(recipient))

    async def send(self, message: Message) -> SendResult:  # pragma: no cover - not wired
        raise NotImplementedError(
            "WhatsApp Cloud API provider is not implemented yet. It requires Meta Business "
            "verification and approved message templates - see CLARIFICATIONS #22."
        )
