"""Push notifications via Expo's hosted push service - the relay for both APNs and FCM,
so this needs no Apple push certificate or Firebase project of its own. `recipient` on
the Message is the device's Expo push token (ExponentPushToken[...]), stored on
mobile_devices.push_token (migration 0067).

Channel mapping: there is no dedicated `Channel.PUSH` member in `providers.py` - the
channel migration 0015 already shipped is `web_push` (`Channel.WEB_PUSH`), used today by
the notification-policy/recipient-group channel lists but never given a provider. This
adapter fills that slot rather than adding a new enum member: an Expo-delivered mobile
push is still, from the policy/escalation-ladder's point of view, "push a notification to
a device" - the same bucket `web_push` already names in the schema. If a *browser* web
push provider (VAPID) is ever added alongside this, that will be the time to split
`web_push` into distinct mobile/web channels; until then this is the one push channel
there is.

Delivery semantics, matching the retryable-vs-permanent split every other adapter here
follows (see providers.py's module docstring):

  - A transport failure (can't reach Expo at all) or a non-200 HTTP response is treated
    as retryable - most likely Expo's problem or a network blip, not a bad token.
  - A per-ticket "DeviceNotRegistered" error is permanent: the token is dead and resending
    to it will never succeed. Logged with a distinct failure_code so the caller (the
    push-channel resolution path, once wired) can clear the stored token instead of
    retrying it forever.
  - Any other per-ticket error (e.g. rate limiting, a malformed message) is treated as
    retryable - conservative, since none of those mean the token itself is bad.
"""
from __future__ import annotations

import logging
import re

import httpx

from csense_shared.notifications.providers import (
    Channel,
    DeliveryOutcome,
    Message,
    SendResult,
    redact,
)

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
REQUEST_TIMEOUT = httpx.Timeout(10.0)

# Expo issues tokens as either "ExponentPushToken[...]" or "ExpoPushToken[...]".
_EXPO_TOKEN_RE = re.compile(r"^Expo(nent)?PushToken\[.+\]$")


class ExpoPushProvider:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        # Not defaulted to a stored `httpx.AsyncClient()` here, deliberately - matching
        # resend_email.py/whatsapp.py's pattern in this same package. `build_registry()`
        # constructs a fresh provider on every call, including per-request (auth.py,
        # memberships.py, reseller.py), not just once at app startup; a client stored in
        # __init__ would mean a new, never-closed connection pool every such call. Only
        # an injected client (this test suite's fake) is held; otherwise one is opened
        # and closed per send via `async with`, same as the other two adapters.
        self._client = http_client

    @property
    def code(self) -> str:
        return "expo_push"

    @property
    def channel(self) -> Channel:
        return Channel.WEB_PUSH

    def validate_recipient(self, recipient: str) -> bool:
        return bool(_EXPO_TOKEN_RE.match(recipient))

    async def send(self, message: Message) -> SendResult:
        payload = {"to": message.recipient, "title": message.subject, "body": message.body}
        try:
            if self._client is not None:
                response = await self._client.post(EXPO_PUSH_URL, json=payload)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.post(EXPO_PUSH_URL, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("expo_push_unreachable", extra={"error": str(exc)[:200]})
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code="unreachable",
                failure_summary=redact(str(exc)),
            )

        if response.status_code != 200:
            logger.warning("expo_push_http_error", extra={"status": response.status_code})
            try:
                body = str(response.json())
            except Exception:  # noqa: BLE001 - body may not be JSON at all on a 5xx
                body = None
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code=f"http_{response.status_code}",
                failure_summary=redact(body),
            )

        data = response.json().get("data", {})
        if data.get("status") != "ok":
            detail = data.get("details") or {}
            error = detail.get("error")
            # DeviceNotRegistered is the case the caller should clear the stored token
            # for - logged here so it's visible, cleared by the caller (the push-channel
            # resolution path), since this provider has no DB access of its own.
            logger.info("expo_push_ticket_error", extra={"detail": detail})
            if error == "DeviceNotRegistered":
                return SendResult(
                    DeliveryOutcome.FAILED_PERMANENT,
                    failure_code="device_not_registered",
                    failure_summary=redact(str(detail)),
                )
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code=str(error) if error else "unknown_ticket_error",
                failure_summary=redact(str(detail)),
            )

        return SendResult(DeliveryOutcome.ACCEPTED)
