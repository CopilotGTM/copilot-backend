"""Shared outbound delivery channel services."""

from src.service.channels.outbound_channel_service import (
    OutboundChannelInfo,
    OutboundChannelSendResult,
    OutboundChannelService,
    OutboundDeliveryTarget,
)

__all__ = [
    "OutboundChannelInfo",
    "OutboundChannelSendResult",
    "OutboundChannelService",
    "OutboundDeliveryTarget",
]
