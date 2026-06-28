"""Shared outbound delivery channel services."""

from src.service.channels.chat_context_service import OutboundChatContextService
from src.service.channels.outbound_channel_service import (
    OutboundChannelInfo,
    OutboundChannelSendResult,
    OutboundChannelService,
    OutboundDeliveryTarget,
)
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
    "OutboundChatContextService",
    "OutboundChannelInfo",
    "OutboundChannelSendResult",
    "OutboundChannelService",
    "OutboundDeliveryTarget",
    "WhatsAppOutboundProvider",
]
