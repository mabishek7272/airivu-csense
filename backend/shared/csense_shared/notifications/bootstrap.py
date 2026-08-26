"""Builds the provider registry from configuration.

Missing credentials are not an error. A deployment legitimately runs email-only, or with
no WhatsApp number linked yet, and refusing to start in that case would make the whole
platform unavailable because one optional channel is unconfigured.

Instead the channel is simply absent from the registry, `send_delivery` records a
`channel_not_configured` failure with a readable reason, and the Developer Console shows
it as unconfigured. The failure is visible where someone will look for it rather than in
a startup log nobody reads.
"""
from __future__ import annotations

import logging

from csense_shared.config import Settings
from csense_shared.notifications.providers import ProviderRegistry
from csense_shared.notifications.resend_email import ResendEmailProvider
from csense_shared.notifications.whatsapp import EvolutionGoWhatsAppProvider

logger = logging.getLogger(__name__)


def build_registry(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()

    if settings.resend_api_key:
        registry.register(
            ResendEmailProvider(
                api_key=settings.resend_api_key,
                from_address=settings.resend_from_address,
                reply_to=settings.resend_reply_to or None,
            )
        )
    else:
        logger.info("email_provider_not_configured", extra={"reason": "RESEND_API_KEY unset"})

    if settings.whatsapp_gateway_url and settings.whatsapp_gateway_api_key:
        registry.register(
            EvolutionGoWhatsAppProvider(
                base_url=settings.whatsapp_gateway_url,
                api_key=settings.whatsapp_gateway_api_key,
                instance=settings.whatsapp_instance,
            )
        )
    else:
        logger.info(
            "whatsapp_provider_not_configured",
            extra={"reason": "WHATSAPP_GATEWAY_URL or WHATSAPP_GATEWAY_API_KEY unset"},
        )

    logger.info("notification_providers_ready", extra={"providers": registry.describe()})
    return registry
