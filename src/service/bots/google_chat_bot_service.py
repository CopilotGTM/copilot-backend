"""Google Chat Bot Service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import aiohttp
from google.auth.transport import requests as google_requests
from sqlalchemy.exc import IntegrityError
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.config.configs import google_chat_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)


@dataclass(frozen=True)
class GoogleChatInboundEvent:
    """Normalized Google Chat interaction event for bot routing."""

    event_type: str
    event_id: str | None
    message_name: str | None
    space_name: str
    thread_name: str | None
    external_user_id: str
    external_tenant_id: str
    text: str
    display_name: str | None = None
    email: str | None = None
    timezone: str | None = None


class GoogleChatBotService:
    """Handle Google Chat interaction events and outbound bot messages."""

    def __init__(self) -> None:
        self._identity_service = ExternalIdentityService()
        self._auth_state_service: BotAuthStateService | None = None

    @property
    def auth_state_service(self) -> BotAuthStateService:
        if self._auth_state_service is None:
            self._auth_state_service = get_bot_auth_state_service(
                login_url_base=google_chat_config.GOOGLE_CHAT_AUTH_LOGIN_URL
            )
        return self._auth_state_service

    @auth_state_service.setter
    def auth_state_service(self, service: BotAuthStateService) -> None:
        self._auth_state_service = service

    async def process_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("type") or payload.get("eventType") or "").upper()

        if event_type == "ADDED_TO_SPACE":
            return self._welcome_message()
        if event_type == "REMOVED_FROM_SPACE":
            logger.info(
                "Google Chat app removed from space=%s", payload.get("space", {}).get("name")
            )
            return {}
        if event_type in {"CARD_CLICKED", "SUBMIT_FORM", "APP_COMMAND"}:
            return {"text": "This Google Chat action is not supported yet. Please send a message."}
        if event_type != "MESSAGE":
            logger.debug("Ignoring unsupported Google Chat event type=%s", event_type)
            return {}

        event = self._extract_event(payload)
        if not event.text:
            return {"text": "I received an empty message. How can I help?"}
        return await self._route_message(event)

    def _extract_event(self, payload: dict[str, Any]) -> GoogleChatInboundEvent:
        event_type = str(payload.get("type") or payload.get("eventType") or "").upper()
        message = payload.get("message") or {}
        space = payload.get("space") or message.get("space") or {}
        user = payload.get("user") or message.get("sender") or {}
        thread = message.get("thread") or {}

        space_name = str(space.get("name") or "")
        external_user_id = str(user.get("name") or user.get("displayName") or "")
        return GoogleChatInboundEvent(
            event_type=event_type,
            event_id=str(payload.get("eventId") or payload.get("event_id") or "") or None,
            message_name=str(message.get("name") or "") or None,
            space_name=space_name,
            thread_name=str(thread.get("name") or "") or None,
            external_user_id=external_user_id,
            external_tenant_id=space_name,
            text=str(message.get("argumentText") or message.get("text") or "").strip(),
            display_name=str(user.get("displayName") or "") or None,
            email=str(user.get("email") or "") or None,
            timezone=str(user.get("timeZone") or user.get("timezone") or "") or None,
        )

    async def _route_message(self, event: GoogleChatInboundEvent) -> dict[str, Any]:
        user, _ = await self._get_linked_user(event.external_user_id, event.external_tenant_id)
        if not user:
            return self._get_login_message_with_state(event)

        from src.workers.chat_worker.client import send_chat_task_async

        job_id: str | None = None
        try:
            job_id = str(await self._create_bot_job(user=user, message_text=event.text))
        except Exception:
            logger.exception("Failed to create Google Chat bot job, dispatching without job_id")

        reply_context = self._build_reply_context(event)
        bot_context: dict[str, Any] = {
            "conversation_type": "personal",
            "external_user_id": event.external_user_id,
            "external_tenant_id": event.external_tenant_id,
            "google_chat_message_name": event.message_name,
            "current_datetime": datetime.now(UTC).isoformat(),
        }
        if event.timezone:
            bot_context["user_timezone"] = event.timezone

        try:
            await send_chat_task_async(
                "chat.process_bot_message",
                args=[
                    str(user.organisation_id),
                    str(user.user_id),
                    event.text,
                    "google_chat",
                    reply_context,
                ],
                kwargs={
                    "user_email": user.email,
                    "bot_context": bot_context,
                    "job_id": job_id,
                },
                queue="chat_messages",
            )
        except Exception as exc:
            logger.error("Failed to enqueue Google Chat bot message task: %s", exc, exc_info=True)
            return {
                "text": (
                    "I'm sorry, I'm having trouble processing your request right now. "
                    "Please try again in a moment."
                )
            }

        return {"text": "Thinking..."}

    async def _get_linked_user(
        self, external_user_id: str, external_tenant_id: str
    ) -> tuple[User | None, dict[str, Any] | None]:
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.GOOGLE_CHAT,
                    external_user_id,
                    external_tenant_id,
                )
                if user and mapping:
                    return user, mapping.conversation_context
                return None, None
        except Exception:
            logger.error("Failed to look up Google Chat identity", exc_info=True)
        return None, None

    def _get_login_message_with_state(self, event: GoogleChatInboundEvent) -> dict[str, Any]:
        if not google_chat_config.GOOGLE_CHAT_AUTH_LOGIN_URL:
            return {
                "text": (
                    "Sign in required.\n\n"
                    "Unable to generate a login link right now. Please contact your administrator."
                )
            }

        state = self.auth_state_service.create_state(
            provider=ExternalIdentityProvider.GOOGLE_CHAT,
            external_user_id=event.external_user_id,
            external_tenant_id=event.external_tenant_id,
            external_user_name=event.display_name,
            context=self._build_reply_context(event),
        )
        login_url = self.auth_state_service.generate_login_url(state)
        if not login_url:
            return {
                "text": (
                    "Sign in required.\n\n"
                    "Unable to generate a login link right now. Please contact your administrator."
                )
            }

        return {
            "text": (
                "Sign in required.\n\n"
                "Please sign in to your CopilotGTM account:\n"
                f"{login_url}\n\n"
                "After signing in, come back here and send your question again.\n\n"
                "This link expires in 5 minutes."
            )
        }

    @staticmethod
    def _build_reply_context(event: GoogleChatInboundEvent) -> dict[str, str]:
        reply_context = {"space_name": event.space_name}
        if event.thread_name:
            reply_context["thread_name"] = event.thread_name
        return reply_context

    @staticmethod
    def _welcome_message() -> dict[str, str]:
        return {
            "text": (
                "Hi, I'm Kai, your CopilotGTM assistant.\n\n"
                "Send me a message and I'll help you find answers from your organization's "
                "knowledge base. If needed, I'll ask you to sign in first."
            )
        }

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
                col(ChatSession.chat_type) == ChatType.GOOGLE_CHAT,
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
                        "chat_type": ChatType.GOOGLE_CHAT,
                        "title": "Google Chat",
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
                await db.flush()
                await db.commit()
                message_id = msg.message_id
                break

        async for db in acquire_org_async_session(org_id):
            job = ChatMessageJob(
                message_id=message_id,
                job_type="google_chat_response",
                chat_type="GOOGLE_CHAT",
                status=JobStatus.PENDING,
                user_id=user.user_id,
                job_metadata={
                    "source": "bot",
                    "provider": "google_chat",
                    "conversation_type": "personal",
                    **({"session_id": str(session_id)} if session_id else {}),
                },
            )
            db.add(job)
            await db.commit()
            logger.info(
                "Created Google Chat bot job=%s message=%s session=%s",
                job.job_id,
                message_id,
                session_id,
            )
            return job.job_id

        raise RuntimeError("Failed to create Google Chat bot job")

    async def send_message(
        self,
        space_name: str,
        text: str,
        thread_name: str | None = None,
    ) -> bool:
        access_token = await self._get_app_access_token()
        if not access_token:
            logger.error("Google Chat app credentials are unavailable")
            return False

        payload: dict[str, Any] = {"text": text}
        params: dict[str, str] = {}
        if thread_name:
            payload["thread"] = {"name": thread_name}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        url = f"{google_chat_config.GOOGLE_CHAT_API_BASE}/{space_name}/messages"
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=google_chat_config.GOOGLE_CHAT_HTTP_TIMEOUT_SECONDS
                    )
                ) as session,
                session.post(
                    url,
                    json=payload,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                ) as response,
            ):
                if response.status < 300:
                    return True
                logger.warning(
                    "Google Chat send failed status=%s body=%s",
                    response.status,
                    await response.text(),
                )
        except Exception:
            logger.warning("Failed to send Google Chat message", exc_info=True)
        return False

    async def _get_app_access_token(self) -> str | None:
        try:
            credentials = google_chat_config.get_app_credentials()
            return await asyncio.to_thread(self._refresh_app_credentials, credentials)
        except Exception:
            logger.warning("Failed to refresh Google Chat app credentials", exc_info=True)
            return None

    @staticmethod
    def _refresh_app_credentials(credentials: Any) -> str | None:
        credentials.refresh(google_requests.Request())
        return cast("str | None", credentials.token)
