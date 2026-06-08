"""Resolve and send through user-linked outbound bot channels."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.adapters import ChannelPayload
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProvider,
)

logger = logging.getLogger(__name__)

SUPPORTED_OUTBOUND_CHANNELS = ("teams", "slack", "whatsapp")

_CHANNEL_METADATA = {
    "teams": {
        "label": "Microsoft Teams",
        "icon_url": "/integrations/microsoft-teams.png",
    },
    "slack": {
        "label": "Slack",
        "icon_url": "/integrations/slack.png",
    },
    "whatsapp": {
        "label": "WhatsApp",
        "icon_url": "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
    },
}
_CHANNEL_TO_PROVIDER = {
    "teams": ExternalIdentityProvider.TEAMS,
    "slack": ExternalIdentityProvider.SLACK,
    "whatsapp": ExternalIdentityProvider.WHATSAPP,
}
_REQUIRED_CONTEXT_KEYS = {
    "teams": ("service_url", "conversation_id"),
    "slack": ("team_id", "channel_id"),
    "whatsapp": ("phone_number_id", "recipient_wa_id"),
}


@dataclass(frozen=True)
class OutboundChannelInfo:
    """Availability summary for one user-linked bot channel."""

    channel: str
    available: bool
    label: str
    icon_url: str
    mapping_id: UUID | None = None


@dataclass(frozen=True)
class OutboundDeliveryTarget:
    """Resolved provider context needed to deliver one outbound message."""

    channel: str
    mapping_id: UUID | None
    delivery_context: dict[str, Any]


@dataclass(frozen=True)
class OutboundChannelSendResult:
    """Per-channel delivery result returned to backend callers."""

    channel: str
    success: bool
    mapping_id: UUID | None = None
    error: str | None = None


class OutboundChannelService:
    """Shared resolver and sender for user-linked bot delivery channels."""

    async def list_user_channels(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        *,
        channels: tuple[str, ...] = SUPPORTED_OUTBOUND_CHANNELS,
    ) -> list[OutboundChannelInfo]:
        """Return availability for supported outbound channels for this user."""
        supported_channels = tuple(channel for channel in channels if channel in _CHANNEL_METADATA)
        stmt = select(
            col(ExternalIdentityMapping.provider),
            col(ExternalIdentityMapping.mapping_id),
            col(ExternalIdentityMapping.conversation_context),
            col(ExternalIdentityMapping.external_tenant_id),
        ).where(
            col(ExternalIdentityMapping.organisation_id) == organisation_id,
            col(ExternalIdentityMapping.user_id) == user_id,
            col(ExternalIdentityMapping.is_active).is_(True),
        ).order_by(
            col(ExternalIdentityMapping.linked_at).desc(),
        )
        result = await master_session.execute(stmt)

        provider_map: dict[str, OutboundChannelInfo] = {
            channel: OutboundChannelInfo(
                channel=channel,
                available=False,
                label=_CHANNEL_METADATA[channel]["label"],
                icon_url=_CHANNEL_METADATA[channel]["icon_url"],
                mapping_id=None,
            )
            for channel in supported_channels
        }

        for row in self._result_rows(result):
            provider = self._provider_name(self._row_value(row, "provider", 0))
            if provider not in provider_map:
                continue

            context = self.normalize_delivery_context(
                provider,
                self._row_value(row, "conversation_context", 2),
                self._row_value(row, "external_tenant_id", 3),
            )
            current = provider_map[provider]
            context_ready = self.delivery_context_is_ready(provider, context)
            provider_map[provider] = OutboundChannelInfo(
                channel=provider,
                available=current.available or context_ready,
                label=current.label,
                icon_url=current.icon_url,
                mapping_id=self._row_value(row, "mapping_id", 1)
                if context_ready and not current.available
                else current.mapping_id,
            )

        return [provider_map[channel] for channel in supported_channels]

    async def resolve_user_channel(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        delivery_channel: str,
    ) -> OutboundDeliveryTarget | None:
        """Resolve a single ready delivery target for a user's channel."""
        provider = _CHANNEL_TO_PROVIDER.get(delivery_channel)
        if provider is None:
            return None

        stmt = select(
            col(ExternalIdentityMapping.mapping_id),
            col(ExternalIdentityMapping.conversation_context),
            col(ExternalIdentityMapping.external_tenant_id),
        ).where(
            col(ExternalIdentityMapping.organisation_id) == organisation_id,
            col(ExternalIdentityMapping.user_id) == user_id,
            col(ExternalIdentityMapping.provider) == provider,
            col(ExternalIdentityMapping.is_active).is_(True),
        ).order_by(
            col(ExternalIdentityMapping.linked_at).desc(),
        )
        result = await master_session.execute(stmt)
        for row in self._result_rows(result):
            context = self.normalize_delivery_context(
                delivery_channel,
                self._row_value(row, "conversation_context", 1, fallback_index=0),
                self._row_value(row, "external_tenant_id", 2, fallback_index=1),
            )
            if not self.delivery_context_is_ready(delivery_channel, context):
                continue

            return OutboundDeliveryTarget(
                channel=delivery_channel,
                mapping_id=self._row_value(row, "mapping_id", 0)
                if not isinstance(row, tuple) or len(row) > 2
                else None,
                delivery_context=context,
            )
        return None

    async def resolve_available_targets(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        *,
        channels: tuple[str, ...] = SUPPORTED_OUTBOUND_CHANNELS,
    ) -> list[OutboundDeliveryTarget]:
        """Resolve all ready delivery targets for a user."""
        targets: list[OutboundDeliveryTarget] = []
        for channel in channels:
            target = await self.resolve_user_channel(
                master_session,
                organisation_id,
                user_id,
                channel,
            )
            if target:
                targets.append(target)
        return targets

    async def send_text_to_targets(
        self,
        targets: list[OutboundDeliveryTarget],
        text: str,
    ) -> list[OutboundChannelSendResult]:
        """Send text to each target, preserving per-channel success/failure."""
        results = []
        for target in targets:
            results.append(await self.send_text(target, text))
        return results

    async def send_text(
        self,
        target: OutboundDeliveryTarget,
        text: str,
    ) -> OutboundChannelSendResult:
        """Send plain text to one resolved outbound target."""
        if target.channel not in _REQUIRED_CONTEXT_KEYS:
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error="Unsupported outbound channel",
            )
        if not self.delivery_context_is_ready(target.channel, target.delivery_context):
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error="Delivery context is incomplete",
            )
        if not text.strip():
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error="Message text is empty",
            )

        try:
            if target.channel == "teams":
                from src.service.bots.teams_bot_service import TeamsBotService

                sent = await TeamsBotService().send_proactive_message(
                    service_url=str(target.delivery_context.get("service_url", "")),
                    conversation_id=str(target.delivery_context.get("conversation_id", "")),
                    message=text,
                )
            elif target.channel == "slack":
                from src.service.bots.slack_bot_service import SlackBotService

                thread_ts = target.delivery_context.get("thread_ts")
                if thread_ts:
                    sent = await SlackBotService()._send_message(
                        str(target.delivery_context.get("team_id", "")),
                        str(target.delivery_context.get("channel_id", "")),
                        text,
                        thread_ts=thread_ts,
                    )
                else:
                    sent = await SlackBotService()._send_message(
                        str(target.delivery_context.get("team_id", "")),
                        str(target.delivery_context.get("channel_id", "")),
                        text,
                    )
            elif target.channel == "whatsapp":
                from src.service.bots.whatsapp_bot_service import WhatsAppBotService
                from src.workers.chat_worker.services.bot_message_processing_service import (
                    BotMessageProcessingService,
                )

                sent = True
                for chunk in BotMessageProcessingService._split_whatsapp_text(text):
                    chunk_sent = await WhatsAppBotService().send_text_message(
                        phone_number_id=str(
                            target.delivery_context.get("phone_number_id", "")
                        ),
                        recipient_wa_id=str(
                            target.delivery_context.get("recipient_wa_id", "")
                        ),
                        text=chunk,
                    )
                    if not chunk_sent:
                        sent = False
                        break
            else:
                return OutboundChannelSendResult(
                    channel=target.channel,
                    mapping_id=target.mapping_id,
                    success=False,
                    error="Unsupported outbound channel",
                )
        except Exception as exc:
            logger.warning(
                "Outbound text send failed channel=%s error_type=%s",
                target.channel,
                exc.__class__.__name__,
            )
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error=exc.__class__.__name__,
            )

        return OutboundChannelSendResult(
            channel=target.channel,
            mapping_id=target.mapping_id,
            success=bool(sent),
            error=None if sent else "Provider send returned false",
        )

    async def send_payload(
        self,
        target: OutboundDeliveryTarget,
        payload: ChannelPayload,
    ) -> OutboundChannelSendResult:
        """Send a rendered channel payload to one resolved outbound target."""
        if target.channel not in _REQUIRED_CONTEXT_KEYS:
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error="Unsupported outbound channel",
            )
        if not self.delivery_context_is_ready(target.channel, target.delivery_context):
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error="Delivery context is incomplete",
            )

        try:
            if target.channel == "teams":
                from src.service.bots.teams_bot_service import TeamsBotService

                sent = await TeamsBotService().send_proactive_payload(
                    service_url=str(target.delivery_context.get("service_url", "")),
                    conversation_id=str(target.delivery_context.get("conversation_id", "")),
                    payload=payload.content,
                    text_fallback=payload.text_fallback,
                )
            elif target.channel == "slack":
                sent = await self._send_slack_payload(target, payload)
            elif target.channel == "whatsapp":
                text = payload.content.get("text") or payload.text_fallback or ""
                return await self.send_text(target, text)
            else:
                return OutboundChannelSendResult(
                    channel=target.channel,
                    mapping_id=target.mapping_id,
                    success=False,
                    error="Unsupported outbound channel",
                )
        except Exception as exc:
            logger.warning(
                "Outbound payload send failed channel=%s error_type=%s",
                target.channel,
                exc.__class__.__name__,
            )
            return OutboundChannelSendResult(
                channel=target.channel,
                mapping_id=target.mapping_id,
                success=False,
                error=exc.__class__.__name__,
            )

        return OutboundChannelSendResult(
            channel=target.channel,
            mapping_id=target.mapping_id,
            success=bool(sent),
            error=None if sent else "Provider send returned false",
        )

    @staticmethod
    async def _send_slack_payload(
        target: OutboundDeliveryTarget,
        payload: ChannelPayload,
    ) -> bool:
        from src.service.bots.slack_bot_service import SlackBotService

        team_id = str(target.delivery_context.get("team_id", ""))
        channel_id = str(target.delivery_context.get("channel_id", ""))
        thread_ts = target.delivery_context.get("thread_ts")
        blocks = payload.content.get("blocks") or []
        attachments = payload.attachments
        text = payload.content.get("text") or payload.text_fallback or ""

        bot_service = SlackBotService()
        if blocks or attachments:
            kwargs: dict[str, Any] = {
                "team_id": team_id,
                "channel_id": channel_id,
                "blocks": blocks,
                "text_fallback": text,
            }
            if attachments:
                kwargs["attachments"] = attachments
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            return await bot_service.send_block_kit_message(**kwargs)
        if thread_ts:
            return await bot_service._send_message(
                team_id,
                channel_id,
                text,
                thread_ts=thread_ts,
            )
        return await bot_service._send_message(team_id, channel_id, text)

    @staticmethod
    def normalize_delivery_context(
        delivery_channel: str,
        conversation_context: dict[str, Any] | None,
        external_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Normalize provider mapping context into send-ready delivery context."""
        normalized = dict(conversation_context) if isinstance(conversation_context, dict) else {}
        if delivery_channel == "slack" and external_tenant_id and not normalized.get("team_id"):
            normalized["team_id"] = external_tenant_id
        return normalized

    @staticmethod
    def delivery_context_is_ready(
        delivery_channel: str,
        delivery_context: dict[str, Any] | None,
    ) -> bool:
        """Return whether a normalized context has required send fields."""
        required_keys = _REQUIRED_CONTEXT_KEYS.get(delivery_channel)
        if not required_keys:
            return False

        context = delivery_context if isinstance(delivery_context, dict) else {}
        return all(bool(str(context.get(key, "")).strip()) for key in required_keys)

    @staticmethod
    def _provider_name(provider: ExternalIdentityProvider | str) -> str:
        return provider.value if isinstance(provider, ExternalIdentityProvider) else str(provider)

    @staticmethod
    def _row_value(
        row: Any,
        name: str,
        index: int,
        *,
        fallback_index: int | None = None,
    ) -> Any:
        row_mapping = getattr(row, "_mapping", None)
        if row_mapping and name in row_mapping:
            return row_mapping[name]
        if hasattr(row, name):
            return getattr(row, name)
        if isinstance(row, tuple):
            tuple_index = fallback_index if fallback_index is not None else index
            if len(row) > tuple_index:
                return row[tuple_index]
        return None

    @staticmethod
    def _result_rows(result: Any) -> list[Any]:
        if hasattr(result, "all"):
            return list(result.all())
        if hasattr(result, "first"):
            row = result.first()
            return [row] if row is not None else []
        return []
