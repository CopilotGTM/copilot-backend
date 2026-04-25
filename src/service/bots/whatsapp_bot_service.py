"""WhatsApp Bot Service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import aiohttp
from sqlalchemy.exc import IntegrityError
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.config.configs import whatsapp_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)

WHATSAPP_GRAPH_API_BASE = "https://graph.facebook.com"


@dataclass
class WhatsAppInboundMessage:
    """Normalized inbound WhatsApp text message."""

    message_id: str
    phone_number_id: str
    recipient_wa_id: str
    text: str
    profile_name: str | None = None


class WhatsAppBotService:
    """Handle WhatsApp webhook traffic and outbound messages."""

    def __init__(self) -> None:
        self._identity_service = ExternalIdentityService()
        self._auth_state_service: BotAuthStateService | None = None

    @property
    def auth_state_service(self) -> BotAuthStateService:
        if self._auth_state_service is None:
            self._auth_state_service = get_bot_auth_state_service(
                login_url_base=whatsapp_config.WHATSAPP_AUTH_LOGIN_URL
            )
        return self._auth_state_service

    async def process_webhook(self, payload: dict[str, Any]) -> None:
        if payload.get("object") != "whatsapp_business_account":
            logger.debug("Ignoring non-WhatsApp payload object=%s", payload.get("object"))
            return

        for message in self._extract_inbound_text_messages(payload):
            await self._route_message(message)

    def _extract_inbound_text_messages(
        self, payload: dict[str, Any]
    ) -> list[WhatsAppInboundMessage]:
        inbound_messages: list[WhatsAppInboundMessage] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = str(value.get("metadata", {}).get("phone_number_id") or "")
                if not phone_number_id:
                    continue

                profile_names = {
                    contact.get("wa_id", ""): contact.get("profile", {}).get("name")
                    for contact in value.get("contacts", [])
                    if contact.get("wa_id")
                }

                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue

                    text = str(message.get("text", {}).get("body") or "").strip()
                    recipient_wa_id = str(message.get("from") or "")
                    message_id = str(message.get("id") or "")
                    if not text or not recipient_wa_id or not message_id:
                        continue

                    inbound_messages.append(
                        WhatsAppInboundMessage(
                            message_id=message_id,
                            phone_number_id=phone_number_id,
                            recipient_wa_id=recipient_wa_id,
                            text=text,
                            profile_name=profile_names.get(recipient_wa_id),
                        )
                    )
        return inbound_messages

    async def _route_message(self, message: WhatsAppInboundMessage) -> None:
        user, _ = await self._get_linked_user(message.recipient_wa_id, message.phone_number_id)
        if not user:
            login_message = self._get_login_message_with_state(message)
            await self.send_text_message(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                text=login_message,
            )
            return

        from src.workers.chat_worker.client import send_chat_task_async

        job_id: str | None = None
        try:
            job_id = str(await self._create_bot_job(user=user, message_text=message.text))
        except Exception:
            logger.exception("Failed to create WhatsApp bot chat job, dispatching without job_id")

        bot_context = {
            "conversation_type": "personal",
            "external_user_id": message.recipient_wa_id,
            "external_tenant_id": message.phone_number_id,
            "current_datetime": datetime.now(UTC).isoformat(),
            "whatsapp_message_id": message.message_id,
        }

        await send_chat_task_async(
            "chat.process_bot_message",
            args=[
                str(user.organisation_id),
                str(user.user_id),
                message.text,
                "whatsapp",
                {
                    "phone_number_id": message.phone_number_id,
                    "recipient_wa_id": message.recipient_wa_id,
                },
            ],
            kwargs={
                "user_email": user.email,
                "bot_context": bot_context,
                "job_id": job_id,
            },
            queue="chat_messages",
        )

    async def _get_linked_user(
        self, recipient_wa_id: str, phone_number_id: str
    ) -> tuple[User | None, dict[str, Any] | None]:
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.WHATSAPP,
                    recipient_wa_id,
                    phone_number_id,
                )
                if user and mapping:
                    return user, mapping.conversation_context
                return None, None
            return None, None
        except Exception:
            logger.error("Failed to look up WhatsApp identity", exc_info=True)
            return None, None

    def _get_login_message_with_state(self, message: WhatsAppInboundMessage) -> str:
        if not whatsapp_config.WHATSAPP_AUTH_LOGIN_URL:
            return (
                "Sign in required.\n\n"
                "Unable to generate a login link right now. Please contact your administrator."
            )

        state = self.auth_state_service.create_state(
            provider=ExternalIdentityProvider.WHATSAPP,
            external_user_id=message.recipient_wa_id,
            external_tenant_id=message.phone_number_id,
            external_user_name=message.profile_name,
            context={
                "phone_number_id": message.phone_number_id,
                "recipient_wa_id": message.recipient_wa_id,
            },
        )
        login_url = self.auth_state_service.generate_login_url(state)
        if not login_url:
            return (
                "Sign in required.\n\n"
                "Unable to generate a login link right now. Please contact your administrator."
            )

        return (
            "Sign in required.\n\n"
            "Please sign in to your CopilotGTM account:\n"
            f"{login_url}\n\n"
            "After signing in, come back here and send your question again.\n\n"
            "This link expires in 5 minutes."
        )

    async def _create_bot_job(self, user: User, message_text: str) -> UUID:
        from sqlmodel import col, select
        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
            ChatMessageRole,
            ChatMessageType,
        )
        from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import acquire_org_async_session

        org_id = user.organisation_id
        session_id: UUID | None = None
        message_id: UUID | None = None

        async for db in acquire_org_async_session(org_id):
            stmt = select(ChatSession).where(
                col(ChatSession.user_id) == user.user_id,
                col(ChatSession.organisation_id) == org_id,
                col(ChatSession.chat_type) == ChatType.WHATSAPP_CHAT,
                col(ChatSession.is_deleted).is_(False),
            )
            result = await db.execute(stmt)
            existing = result.scalars().first()
            if existing:
                session_id = existing.session_id
            else:
                new_session = ChatSession.model_validate(
                    {
                        "user_id": user.user_id,
                        "organisation_id": org_id,
                        "chat_type": ChatType.WHATSAPP_CHAT,
                        "title": "WhatsApp Chat",
                    }
                )
                db.add(new_session)
                try:
                    await db.commit()
                    session_id = new_session.session_id
                except IntegrityError:
                    await db.rollback()
                    result = await db.execute(stmt)
                    existing = result.scalars().first()
                    session_id = existing.session_id if existing else None
            break

        if session_id:
            async for db in acquire_org_async_session(org_id):
                msg = ChatMessage(
                    session_id=session_id,
                    role=ChatMessageRole.USER,
                    message_type=ChatMessageType.GENERAL_MESSAGE,
                    content={"text": message_text},
                )
                db.add(msg)
                await db.commit()
                message_id = msg.message_id
                break

        async for db in acquire_org_async_session(org_id):
            job = ChatMessageJob(
                message_id=message_id,
                job_type="whatsapp_chat_response",
                chat_type="WHATSAPP_CHAT",
                status=JobStatus.PENDING,
                user_id=user.user_id,
                job_metadata={
                    "source": "bot",
                    "provider": "whatsapp",
                    "conversation_type": "personal",
                    **({"session_id": str(session_id)} if session_id else {}),
                },
            )
            db.add(job)
            await db.commit()
            logger.info(
                "Created WhatsApp bot job=%s message=%s session=%s",
                job.job_id,
                message_id,
                session_id,
            )
            return job.job_id

        raise RuntimeError("Failed to create WhatsApp bot job")

    async def send_text_message(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> bool:
        access_token = await whatsapp_config.get_access_token()
        if not access_token:
            logger.error("WhatsApp access token not available")
            return False

        url = (
            f"{WHATSAPP_GRAPH_API_BASE}/{whatsapp_config.GRAPH_API_VERSION}/"
            f"{phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_wa_id,
            "type": "text",
            "text": {"body": text},
        }

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
                if response.status < 300:
                    return True

                logger.warning(
                    "WhatsApp send failed status=%s body=%s",
                    response.status,
                    await response.text(),
                )
                return False
        except Exception:
            logger.warning("Failed to send WhatsApp message", exc_info=True)
            return False
