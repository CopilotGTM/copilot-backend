"""WhatsApp Cloud API outbound delivery provider."""

from __future__ import annotations

from typing import Any

import aiohttp
from src.infra.config.configs import whatsapp_config
from src.infra.utils.logger import get_logger
from src.service.bots.whatsapp_bot_service import WHATSAPP_GRAPH_API_BASE

logger = get_logger(__name__)

DEFAULT_TEMPLATE_LANGUAGE = "en_US"
MEETING_SUMMARY_TEMPLATE_NAME = "meeting_summary_snapshot"
TASK_REMINDER_TEMPLATE_NAME = "task_reminder"


class WhatsAppOutboundProvider:
    """Send outbound WhatsApp template messages through Meta Cloud API."""

    async def send_text_message_with_id(
        self,
        *,
        phone_number_id: str,
        recipient_wa_id: str,
        text: str,
    ) -> str | None:
        """Send freeform text and return Meta's message id when accepted."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": recipient_wa_id,
            "type": "text",
            "text": {"body": text},
        }
        response_payload = await self._post_message_payload(phone_number_id, payload)
        if not response_payload:
            return None
        messages = response_payload.get("messages") or []
        if not messages:
            return None
        return str(messages[0].get("id") or "") or None

    async def send_template_message_with_id(
        self,
        *,
        phone_number_id: str,
        recipient_wa_id: str,
        template_name: str,
        language_code: str = DEFAULT_TEMPLATE_LANGUAGE,
        body_parameters: list[str] | None = None,
    ) -> str | None:
        """Send a WhatsApp approved template and return Meta's message id."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": recipient_wa_id,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if body_parameters:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(parameter)} for parameter in body_parameters
                    ],
                }
            ]

        response_payload = await self._post_message_payload(phone_number_id, payload)
        if not response_payload:
            return None
        messages = response_payload.get("messages") or []
        if not messages:
            return None
        return str(messages[0].get("id") or "") or None

    async def send_template_message(
        self,
        *,
        phone_number_id: str,
        recipient_wa_id: str,
        template_name: str,
        language_code: str = DEFAULT_TEMPLATE_LANGUAGE,
        body_parameters: list[str] | None = None,
    ) -> bool:
        """Send a template and return whether Meta accepted it."""
        return bool(
            await self.send_template_message_with_id(
                phone_number_id=phone_number_id,
                recipient_wa_id=recipient_wa_id,
                template_name=template_name,
                language_code=language_code,
                body_parameters=body_parameters,
            )
        )

    async def _post_message_payload(
        self,
        phone_number_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        access_token = await whatsapp_config.get_access_token()
        if not access_token:
            logger.error("WhatsApp access token not available")
            return None

        url = (
            f"{WHATSAPP_GRAPH_API_BASE}/{whatsapp_config.GRAPH_API_VERSION}/"
            f"{phone_number_id}/messages"
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                ) as response,
            ):
                body = await response.json(content_type=None)
                if response.status < 300:
                    return body
                logger.warning(
                    "WhatsApp template send failed status=%s body=%s",
                    response.status,
                    body,
                )
                return None
        except Exception:
            logger.warning("Failed to send WhatsApp template message", exc_info=True)
            return None
