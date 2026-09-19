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

import base64
import html
import logging
from email.utils import parseaddr
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
# Resend caps at 40 MB per message. Alert snapshots are a couple of hundred KB each and
# at most three are attached, so the cap is not a practical constraint.
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

    def _from_header(self, message: Message) -> str:
        """The sending *address* is always the one configured process-wide (shared
        Resend domain, no per-brand SPF/DKIM needed) - only the display *name* varies
        per message. `parseaddr` handles `self._from` being configured either as a bare
        address or an already-"Name <addr>" string, so a global default name/address
        keeps working unchanged when `message.from_name` is absent."""
        if not message.from_name:
            return self._from
        _, address = parseaddr(self._from)
        return f"{message.from_name} <{address or self._from}>"

    async def send(self, message: Message) -> SendResult:
        payload: dict[str, Any] = {
            "from": self._from_header(message),
            "to": [message.recipient],
            "subject": message.subject or "CSense alert",
            "html": self._render_html(message),
            "text": message.body,
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        if message.attachments:
            # Attached rather than linked. Gmail proxies `<img src>` through Google's
            # fetchers, so a linked snapshot would have to be publicly reachable; an
            # attachment needs no public host at all, and it still opens months later
            # when any presigned URL would long since have expired.
            payload["attachments"] = [
                {
                    "filename": attachment.filename,
                    "content": base64.b64encode(attachment.content).decode(),
                }
                for attachment in message.attachments[:3]
            ]

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

        Snapshots are referenced by `cid:` against the attachments, never by external URL,
        so nothing here depends on publicly reachable storage.

        `brand_logo_url`, unlike the snapshot images above, IS a plain external URL, not
        a `cid:` attachment - deliberately: it points at `BUCKET_BRANDING`, which is
        public-read specifically so a logo in a months-old alert email keeps rendering
        long after any presigned URL would have expired (see objects.py's own module
        docstring for the full reasoning). `brand_footer_text` replaces the hardcoded
        AIRIVU/CSense credit for a branded tenant - for the default (unbranded) case
        both are `None` and this renders exactly as it always has.
        """
        body_html = html.escape(message.body).replace("\n", "<br>")
        images = "".join(
            f'<img src="cid:{html.escape(attachment.filename)}" alt="Detection snapshot" '
            f'style="max-width:100%;border-radius:6px;margin-top:16px">'
            for attachment in message.attachments[:3]
        )
        logo_html = (
            f'<img src="{html.escape(message.brand_logo_url)}" alt="" '
            f'style="max-height:32px;margin-bottom:16px">'
            if message.brand_logo_url
            else ""
        )
        footer_text = html.escape(
            message.brand_footer_text
            or "Sent by AIRIVU CSense. Reply to this message to acknowledge the incident."
        )
        return (
            '<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
            'font-size:15px;line-height:1.55;color:#14181f;max-width:600px">'
            f"{logo_html}"
            f"<p>{body_html}</p>{images}"
            '<hr style="border:none;border-top:1px solid #d5d9e0;margin:24px 0">'
            f'<p style="font-size:12px;color:#545c6a">{footer_text}</p></div>'
        )
