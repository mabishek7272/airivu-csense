"""Notification provider adapters (TRD §18).

Every channel implements one interface, so the notification service never learns which
vendor is behind a channel. That is not abstraction for its own sake:

  - WhatsApp today is Evolution Go, an unofficial client whose numbers can be banned
    without warning. Meta's official Cloud API is the eventual replacement. Behind this
    interface that is a config change, not a rewrite (CLARIFICATIONS #22).
  - Email is Resend, which is excellent and also a single point of failure. A second
    provider becomes a fallback rather than a migration.

Adapters return a `SendResult` instead of raising for delivery problems, because the
distinction that matters operationally is **retryable vs permanent**. A 429 or a 502 is
worth retrying; "invalid recipient" never is, and retrying it forever hides a bad address
behind a queue that never drains.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    WEB_PUSH = "web_push"
    WEBHOOK = "webhook"


class DeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"      # provider took it; final state arrives by webhook
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"


@dataclass(frozen=True)
class Attachment:
    """Bytes carried with the message rather than linked from it."""

    filename: str
    content: bytes
    mime_type: str = "image/jpeg"


@dataclass(frozen=True)
class Message:
    """One message to one recipient, already rendered.

    Media is carried two ways because the channels consume it differently, and getting
    this wrong is the difference between an alert with a picture and one without:

      `media_urls` - for channels whose *server* fetches the URL. The WhatsApp gateway
          downloads the media itself, from inside our network, then uploads the bytes to
          WhatsApp. So these can be internal URLs; they never leave the deployment.

      `attachments` - for channels that must carry the bytes. Email is the case that
          matters: Gmail proxies `<img src>` through Google's own fetchers, so a linked
          image would need to be publicly reachable. Attaching sidesteps that entirely,
          and an attached snapshot survives in the mailbox after any URL would have
          expired.
    """

    recipient: str            # email address, E.164 number, user id, or URL
    subject: str | None
    body: str
    media_urls: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Idempotency key. Providers that support it will not double-send on our retry.
    idempotency_key: str | None = None
    metadata: dict = field(default_factory=dict)
    # White-label branding (all optional; None means "use the default CSense look").
    # Populated by notification_worker's process_delivery() from org_branding_resolve()
    # when the tenant this delivery belongs to has branding configured - see that
    # module's own docstring for why the lookup happens there and not in dispatcher.py.
    # Only ResendEmailProvider consumes these today (channels without a "from name" or
    # a template header concept simply ignore them).
    from_name: str | None = None
    brand_logo_url: str | None = None
    brand_footer_text: str | None = None


@dataclass(frozen=True)
class SendResult:
    outcome: DeliveryOutcome
    provider_message_id: str | None = None
    failure_code: str | None = None
    # Must be safe to persist and show: providers echo the recipient address and
    # occasionally fragments of credentials in error bodies (TRD-SEC-005).
    failure_summary: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is DeliveryOutcome.ACCEPTED

    @property
    def should_retry(self) -> bool:
        return self.outcome is DeliveryOutcome.FAILED_RETRYABLE


class NotificationProvider(Protocol):
    """What every channel adapter must implement."""

    @property
    def code(self) -> str:
        """Stable identifier recorded on each delivery, e.g. 'resend'."""

    @property
    def channel(self) -> Channel: ...

    def validate_recipient(self, recipient: str) -> bool:
        """Cheap syntactic check, before spending an API call on a bad address."""

    async def send(self, message: Message) -> SendResult: ...


# --- Redaction ---------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+\d{7,15}")
# Bearer tokens and provider keys that turn up in echoed request bodies.
_SECRET_RE = re.compile(r"(?i)(bearer\s+|re_|sk_|key[-_]?)[A-Za-z0-9_\-]{8,}")


def redact(text: str | None, limit: int = 400) -> str | None:
    """Strips addresses and credentials from provider errors before they are stored.

    Error bodies routinely contain the recipient address, and sometimes the API key that
    was rejected. Both would otherwise end up in a database column that support staff and
    log aggregators can read (TRD-SEC-005).
    """
    if not text:
        return None
    cleaned = _SECRET_RE.sub("[redacted-credential]", text)
    cleaned = _EMAIL_RE.sub("[redacted-email]", cleaned)
    cleaned = _PHONE_RE.sub("[redacted-phone]", cleaned)
    return cleaned[:limit]


def mask_recipient(recipient: str) -> str:
    """Display form for an address: enough to recognise, not enough to harvest."""
    if "@" in recipient:
        local, _, domain = recipient.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}{'*' * max(1, len(local) - len(head))}@{domain}"
    if recipient.startswith("+"):
        return f"{recipient[:3]}{'*' * max(0, len(recipient) - 6)}{recipient[-3:]}"
    return recipient


# --- Registry -----------------------------------------------------------------------

class ProviderRegistry:
    """Resolves a channel to its configured provider.

    A channel with no provider is not an error at registration time - a deployment
    legitimately runs without SMS. It becomes an error only when something tries to send,
    and then it is a permanent failure with a clear reason rather than a crash.
    """

    def __init__(self) -> None:
        self._providers: dict[Channel, NotificationProvider] = {}

    def register(self, provider: NotificationProvider) -> None:
        self._providers[provider.channel] = provider

    def get(self, channel: Channel | str) -> NotificationProvider | None:
        """Accepts a Channel or its string value, so API handlers can look up by the
        name that arrived on the wire without converting first."""
        if isinstance(channel, str):
            try:
                channel = Channel(channel)
            except ValueError:
                return None
        return self._providers.get(channel)

    def available_channels(self) -> list[Channel]:
        return sorted(self._providers.keys())

    def describe(self) -> dict[str, str]:
        """For the console's provider panel: which channel is served by what."""
        return {str(channel): provider.code for channel, provider in self._providers.items()}
