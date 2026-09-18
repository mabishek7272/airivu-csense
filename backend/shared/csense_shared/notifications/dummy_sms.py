"""Dummy SMS provider for development and testing.

Logs SMS messages to console instead of sending to a real provider. Useful when no SMS
vendor is configured, or during development. Production should swap this for a real
provider (Twilio, Vonage, AWS SNS, etc.) by implementing the same interface.
"""
from __future__ import annotations

import logging
import re

from csense_shared.notifications.providers import (
    Channel,
    DeliveryOutcome,
    Message,
    SendResult,
    redact,
)

logger = logging.getLogger(__name__)

# E.164 phone number format: + followed by 1-15 digits
_PHONE_RE = re.compile(r"^\+\d{1,15}$")


class DummySmsProvider:
    """Development SMS provider that logs to console and marks as accepted."""

    def __init__(self) -> None:
        pass

    @property
    def code(self) -> str:
        return "dummy_sms"

    @property
    def channel(self) -> Channel:
        return Channel.SMS

    def validate_recipient(self, recipient: str) -> bool:
        """Check if the recipient is a valid E.164 phone number."""
        return bool(_PHONE_RE.match(recipient))

    async def send(self, message: Message) -> SendResult:
        """Log the message to console and mark as accepted.

        In production, this would send to a real SMS provider API.
        """
        logger.info(
            "sms_dummy_send",
            extra={
                "recipient": message.recipient,
                "subject": message.subject,
                "body": redact(message.body),
                "attachments_count": len(message.attachments),
            },
        )
        # Accepted, not delivered—consistent with email provider behavior.
        # A real provider would wait for a webhook to confirm actual delivery.
        return SendResult(DeliveryOutcome.ACCEPTED, provider_message_id=f"dummy-{id(message)}")
