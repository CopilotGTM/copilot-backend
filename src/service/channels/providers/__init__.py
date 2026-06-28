"""Provider-specific outbound channel implementations."""

from src.service.channels.providers.whatsapp_outbound_provider import (
    DEFAULT_TEMPLATE_LANGUAGE,
    MEETING_SUMMARY_TEMPLATE_NAME,
    TASK_REMINDER_TEMPLATE_NAME,
    WhatsAppOutboundProvider,
)

__all__ = [
    "DEFAULT_TEMPLATE_LANGUAGE",
    "MEETING_SUMMARY_TEMPLATE_NAME",
    "TASK_REMINDER_TEMPLATE_NAME",
    "WhatsAppOutboundProvider",
]
