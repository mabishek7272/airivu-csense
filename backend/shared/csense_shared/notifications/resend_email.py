"""Resend email provider.

Resend's API is small and its failure modes are well defined, which is what this adapter
leans on: the status code tells us definitively whether a retry can help.

  422 - the address is malformed or the domain is not verified. Permanent. Retrying an
        unverified sending domain forever is how a queue silently fills with work that
        can never succeed.
  429 - rate limited. Retryable, and the only case where backing off actually helps.
  5xx - Resend's problem. Retryable.

Delivery is *not* confirmed by a 200 here. A 200 means Resend accepted the message; the
real outcome (delivered, bounced, complained) arrives later by webhook. Treating
acceptance as delivery is how a system reports 100% success while every message bounces.
"""
from __future__ import annotations

import html
import logging
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

API_URL = "https://api.resend.com/emails"
# Resend caps at 40 MB per message; well under that, since alert emails carry one or two
# snapshots as links rather than attachments.
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class ResendEmailProvider:
    def __init__(
        self,
        api_key: str,
        from_address: str,
        *,
        reply_to: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Resend API key is required")
        self._api_key = api_key
        self._from = from_address
        # Points at the Cloudflare Email Routing address, so replying to an alert reaches
        # the inbound webhook rather than a mailbox nobody watches.
        self._reply_to = reply_to
        self._client = client

    @property
    def code(self) -> str:
        return "resend"

    @property
    def channel(self) -> Channel:
        return Channel.EMAIL

    def validate_recipient(self, recipient: str) -> bool:
        # Deliberately loose: real addresses break naive regexes, and Resend is the
        # authority. This only catches obvious nonsense before spending an API call.
        return "@" in recipient and "." in recipient.split("@")[-1] and " " not in recipient

    async def send(self, message: Message) -> SendResult:
        payload: dict[str, Any] = {
            "from": self._from,
            "to": [message.recipient],
            "subject": message.subject or "CSense alert",
            "html": self._render_html(message),
            "text": message.body,
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        if message.metadata:
            # Resend tags must be ASCII key/value; used to correlate webhooks back to a
            # delivery row without trusting the provider's own id alone.
            payload["tags"] = [
                {"name": str(k)[:32], "value": str(v)[:32]}
                for k, v in list(message.metadata.items())[:5]
            ]

        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key

        try:
            if self._client is not None:
                response = await self._client.post(API_URL, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.post(API_URL, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            # A timeout is ambiguous - the message may well have been sent. Retryable, and
            # the idempotency key is what stops the retry duplicating it.
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code="timeout",
                failure_summary=redact(str(exc)),
            )
        except httpx.HTTPError as exc:
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code="transport_error",
                failure_summary=redact(str(exc)),
            )

        return self._interpret(response)

    def _interpret(self, response: httpx.Response) -> SendResult:
        if response.status_code in (200, 201):
            body = self._safe_json(response)
            message_id = body.get("id") if isinstance(body, dict) else None
            # Accepted, not delivered. The webhook decides the final state.
            return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id=message_id)

        summary = redact(response.text)

        if response.status_code == 429:
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE, failure_code="rate_limited", failure_summary=summary
            )
        if response.status_code >= 500:
            return SendResult(
                DeliveryOutcome.FAILED_RETRYABLE,
                failure_code=f"provider_{response.status_code}",
                failure_summary=summary,
            )
        if response.status_code in (401, 403):
            # Misconfiguration, not a transient fault. Retrying cannot fix a bad key, and
            # doing so buries the real problem under a growing retry queue.
            logger.error("resend_auth_rejected", extra={"status": response.status_code})
            return SendResult(
                DeliveryOutcome.FAILED_PERMANENT,
                failure_code="provider_auth_rejected",
                failure_summary="Resend rejected the API key or sending domain.",
            )
        return SendResult(
            DeliveryOutcome.FAILED_PERMANENT,
            failure_code=f"rejected_{response.status_code}",
            failure_summary=summary,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            return response.json()
        except ValueError:
            return {}

    def _render_html(self, message: Message) -> str:
        """Minimal, table-free HTML.

        Everything is escaped: the body carries a camera name and site name that a tenant
        controls, so an unescaped template would let one tenant inject markup into a mail
        their own staff read.
        """
        body_html = html.escape(message.body).replace("\n", "<br>")
        images = "".join(
            f'<img src="{html.escape(url)}" alt="Detection snapshot" '
            f'style="max-width:100%;border-radius:6px;margin-top:16px">'
            for url in message.media_urls[:3]
        )
        return (
            '<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
            'font-size:15px;line-height:1.55;color:#14181f;max-width:600px">'
            f"<p>{body_html}</p>{images}"
            '<hr style="border:none;border-top:1px solid #d5d9e0;margin:24px 0">'
            '<p style="font-size:12px;color:#545c6a">'
            "Sent by AIRIVU CSense. Reply to this message to acknowledge the incident."
            "</p></div>"
        )
