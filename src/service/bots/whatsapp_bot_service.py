"""WhatsApp bot service orchestration.

This service is the runtime entry-point for parsed WhatsApp webhook events.
It maps inbound messages to internal users, normalizes media payloads, creates
chat jobs, and enqueues `chat.process_bot_message` with WhatsApp-specific
context.

Callers:
- src.api.routers.bots.whatsapp_webhook_handler -> process_webhook
- External ChatAttachment pipeline -> ingest_whatsapp_media_files

Notes:
- Organization isolation is enforced where DB sessions are opened.
- Webhook identity is not trusted by external IDs alone; internal mapping determines
  who can continue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
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
class WhatsAppInboundMedia:
    """Downloadable WhatsApp media that should enter the attachment pipeline."""

    media_id: str
    media_type: str
    mimetype: str
    filename: str
    message_id: str
    caption: str | None = None


@dataclass
class WhatsAppInboundMessage:
    """Normalized inbound WhatsApp message for text, media, and audio inputs."""

    message_id: str
    phone_number_id: str
    recipient_wa_id: str
    text: str
    profile_name: str | None = None
    audio_files: list[dict[str, str]] | None = None
    media_files: list[WhatsAppInboundMedia] | None = None
    unsupported_reason: str | None = None


class WhatsAppBotService:
    """Handle WhatsApp message intake and bot job dispatch.

    The service owns:
    - message-type classification (text, media, location/contact, unsupported)
    - login prompt routing for unlinked external ids
    - media ingestion into the shared attachment boundary
    - creation of personal WhatsApp chat sessions/jobs
    """

    def __init__(self) -> None:
        self._identity_service = ExternalIdentityService()
        self._auth_state_service: BotAuthStateService | None = None

    @property
    def auth_state_service(self) -> BotAuthStateService:
        """Lazy-load auth state service configured for WhatsApp links."""
        if self._auth_state_service is None:
            self._auth_state_service = get_bot_auth_state_service(
                login_url_base=whatsapp_config.WHATSAPP_AUTH_LOGIN_URL
            )
        return self._auth_state_service

    async def process_webhook(self, payload: dict[str, Any]) -> None:
        """Process a verified WhatsApp payload from the webhook router.

        Only `whatsapp_business_account` envelopes are expected. For each parsed
        inbound message we route identity, media, and reply context through the
        same service pipeline.
        """
        if payload.get("object") != "whatsapp_business_account":
            logger.debug("Ignoring non-WhatsApp payload object=%s", payload.get("object"))
            return

        for message in self._extract_inbound_messages(payload):
            await self._route_message(message)

    def _extract_inbound_text_messages(
        self, payload: dict[str, Any]
    ) -> list[WhatsAppInboundMessage]:
        """Backward-compatible alias for extracting inbound messages."""
        return self._extract_inbound_messages(payload)

    def _extract_inbound_messages(self, payload: dict[str, Any]) -> list[WhatsAppInboundMessage]:
        # Parse only object payloads emitted by WhatsApp conversations.
        # Status-only entries are ignored because they do not contain user content.
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
                    message_type = str(message.get("type") or "")
                    recipient_wa_id = str(message.get("from") or "")
                    message_id = str(message.get("id") or "")
                    if not recipient_wa_id or not message_id:
                        continue
                    text = str(message.get("text", {}).get("body") or "").strip()
                    audio_files: list[dict[str, str]] | None = None
                    media_files: list[WhatsAppInboundMedia] | None = None
                    unsupported_reason: str | None = None

                    # WhatsApp sends one message object with a `type` field.
                    # Unknown types are captured as unsupported and reported back explicitly.
                    if message_type == "text":
                        if not text:
                            continue
                    elif message_type in {"audio", "voice"}:
                        # Audio messages are routed as transcription-ready metadata.
                        # Inbound voice/audio ids are later converted into queue context.
                        audio_info = message.get(message_type) or message.get("audio") or {}
                        media_id = str(audio_info.get("id") or "")
                        if not media_id:
                            continue
                        mimetype = str(audio_info.get("mime_type") or "")
                        audio_files = [
                            {
                                "media_id": media_id,
                                "mimetype": mimetype,
                                "filename": self._whatsapp_audio_filename(mimetype),
                                "message_id": message_id,
                            }
                        ]
                    elif message_type in {"image", "document"}:
                        # Documents/images are normalized into Attachment objects and ingested
                        # through the shared external attachment pipeline.
                        media_info = message.get(message_type) or {}
                        media_id = str(media_info.get("id") or "")
                        if not media_id:
                            continue
                        caption = str(media_info.get("caption") or "").strip()
                        mimetype = str(media_info.get("mime_type") or "").strip()
                        filename = str(media_info.get("filename") or "").strip()
                        if not filename:
                            filename = self._whatsapp_media_filename(message_type, mimetype)
                        text = caption
                        media_files = [
                            WhatsAppInboundMedia(
                                media_id=media_id,
                                media_type=message_type,
                                mimetype=mimetype,
                                filename=filename,
                                caption=caption or None,
                                message_id=message_id,
                            )
                        ]
                    elif message_type == "contacts":
                        # Contact payloads are flattened into plain text so worker prompts
                        # can include them alongside normal question text.
                        text = self._format_contact_cards(message.get("contacts") or [])
                        if not text:
                            continue
                    elif message_type == "location":
                        # Location payloads are also plain text for predictable downstream
                        # parsing in the general agent.
                        text = self._format_location(message.get("location") or {})
                        if not text:
                            continue
                    else:
                        unsupported_reason = message_type or "unknown"

                    inbound_messages.append(
                        WhatsAppInboundMessage(
                            message_id=message_id,
                            phone_number_id=phone_number_id,
                            recipient_wa_id=recipient_wa_id,
                            text=text,
                            profile_name=profile_names.get(recipient_wa_id),
                            audio_files=audio_files,
                            media_files=media_files,
                            unsupported_reason=unsupported_reason,
                        )
                    )
        return inbound_messages

    @staticmethod
    def _whatsapp_audio_filename(mimetype: str | None) -> str:
        """Build a stable audio filename when WhatsApp omits a filename."""
        clean_mimetype = (mimetype or "").split(";", maxsplit=1)[0].strip().lower()
        extension = {
            "audio/aac": "aac",
            "audio/amr": "amr",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/ogg": "ogg",
        }.get(clean_mimetype, "ogg")
        return f"whatsapp-audio.{extension}"

    @staticmethod
    def _whatsapp_media_filename(media_type: str, mimetype: str | None) -> str:
        """Build a stable filename from MIME when WhatsApp payload omits one."""
        clean_mimetype = (mimetype or "").split(";", maxsplit=1)[0].strip().lower()
        extension = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "application/pdf": "pdf",
            "text/plain": "txt",
        }.get(clean_mimetype)
        if not extension:
            extension = clean_mimetype.rsplit("/", maxsplit=1)[-1] if "/" in clean_mimetype else ""
        return f"whatsapp-{media_type}{f'.{extension}' if extension else ''}"

    @staticmethod
    def _format_contact_cards(contacts: list[dict[str, Any]]) -> str:
        """Format WhatsApp shared contacts into readable text for prompt context."""
        blocks: list[str] = []
        for contact in contacts:
            name = contact.get("name") or {}
            org = contact.get("org") or {}
            lines = ["[Shared WhatsApp contact]"]
            formatted_name = str(name.get("formatted_name") or "").strip()
            if formatted_name:
                lines.append(f"Name: {formatted_name}")
            company = str(org.get("company") or "").strip()
            if company:
                lines.append(f"Company: {company}")
            phones = [
                str(phone.get("phone") or "").strip()
                for phone in contact.get("phones") or []
                if phone.get("phone")
            ]
            if phones:
                lines.append(f"Phones: {', '.join(phones)}")
            emails = [
                str(email.get("email") or "").strip()
                for email in contact.get("emails") or []
                if email.get("email")
            ]
            if emails:
                lines.append(f"Emails: {', '.join(emails)}")
            if len(lines) > 1:
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @staticmethod
    def _format_location(location: dict[str, Any]) -> str:
        """Format WhatsApp location payload into compact, plain-text context."""
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            return ""

        lines = [
            "[Shared WhatsApp location]",
            f"Latitude: {latitude}",
            f"Longitude: {longitude}",
        ]
        name = str(location.get("name") or "").strip()
        if name:
            lines.append(f"Name: {name}")
        address = str(location.get("address") or "").strip()
        if address:
            lines.append(f"Address: {address}")
        return "\n".join(lines)

    async def _route_message(self, message: WhatsAppInboundMessage) -> None:
        """Route one normalized message to login, job creation, and enqueue path."""
        user, _ = await self._get_linked_user(
            message.recipient_wa_id,
            message.phone_number_id,
            inbound_message_id=message.message_id,
        )
        if not user:
            login_message = self._get_login_message_with_state(message)
            # Unlinked numbers cannot be trusted for chat history, so we force a
            # re-auth flow before any worker enqueue.
            await self.send_text_message(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                text=login_message,
            )
            return

        from src.workers.chat_worker.client import send_chat_task_async

        if message.unsupported_reason and not (
            message.text.strip() or message.media_files or message.audio_files
        ):
            # Explicitly unsupported media should be safe-replied and never
            # enqueued into agent jobs.
            await self.send_text_message(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                text=self._unsupported_message_text(message.unsupported_reason),
            )
            return

        try:
            # Non-blocking typing signal improves UX; failure does not block enqueue.
            await self.send_typing_indicator(
                phone_number_id=message.phone_number_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.warning("Failed to send WhatsApp typing indicator", exc_info=True)

        attachment_refs: list[dict[str, Any]] = []
        if message.media_files:
            # Ingest media first so the worker gets stable attachment refs.
            attachment_refs = await self._ingest_media_files(user, message.media_files)

        job_id: str | None = None
        try:
            job_id = str(
                await self._create_bot_job(
                    user=user,
                    message_text=message.text,
                    attachment_refs=attachment_refs,
                )
            )
        except Exception:
            logger.exception("Failed to create WhatsApp bot chat job, dispatching without job_id")

        bot_context: dict[str, Any] = {
            "conversation_type": "personal",
            "external_user_id": message.recipient_wa_id,
            "external_tenant_id": message.phone_number_id,
            "current_datetime": datetime.now(UTC).isoformat(),
            "whatsapp_message_id": message.message_id,
        }
        if getattr(user, "timezone", None):
            bot_context["user_timezone"] = str(user.timezone)
        if message.audio_files:
            bot_context["whatsapp_audio_files"] = message.audio_files
        if attachment_refs:
            bot_context["current_attachments"] = attachment_refs

        try:
            # Enqueue keeps the webhook fast; the chat worker owns heavy processing.
            await send_chat_task_async(
                "chat.process_bot_message",
                args=[
                    str(user.organisation_id),
                    str(user.user_id),
                    message.text,
                    "whatsapp",
                    self._build_reply_context(
                        phone_number_id=message.phone_number_id,
                        recipient_wa_id=message.recipient_wa_id,
                        incoming_message_id=message.message_id,
                    ),
                ],
                kwargs={
                    "user_email": user.email,
                    "bot_context": bot_context,
                    "job_id": job_id,
                },
                queue="chat_messages",
            )
        except Exception as e:
            logger.error("Failed to enqueue WhatsApp bot message task: %s", e, exc_info=True)
            # Keep the request user-safe if queueing fails after all prior steps.
            await self.send_text_message(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                text=(
                    "I'm sorry, I'm having trouble processing your request right now. "
                    "Please try again in a moment."
                ),
            )

    @staticmethod
    def _build_reply_context(
        phone_number_id: str,
        recipient_wa_id: str,
        incoming_message_id: str | None,
    ) -> dict[str, str]:
        """Build WhatsApp reply context for outbound API calls."""
        reply_context = {
            "phone_number_id": phone_number_id,
            "recipient_wa_id": recipient_wa_id,
        }
        if incoming_message_id:
            reply_context["incoming_message_id"] = incoming_message_id
        return reply_context

    async def _ingest_media_files(
        self, user: User, media_files: list[WhatsAppInboundMedia]
    ) -> list[dict[str, Any]]:
        # Shared ingest service is responsible for download + storage + attachment
        # records so bot attachment behavior stays aligned across channels.
        from src.infra.database.sessions import acquire_org_async_session
        from src.service.chat.chat_attachment_manifest import serialize_attachment_context
        from src.service.chat.external_chat_attachment_ingest_service import (
            ExternalChatAttachmentIngestService,
        )

        access_token = await whatsapp_config.get_access_token()
        if not access_token:
            logger.warning("Skipping WhatsApp media ingestion: access token unavailable")
            return []

        async for db in acquire_org_async_session(user.organisation_id):
            # Keep DB connection ownership inside this boundary; all attachment
            # rows are written only under the caller's organization.
            ingest_service = ExternalChatAttachmentIngestService(
                org_session=db,
                organisation_id=user.organisation_id,
                user_id=user.user_id,
            )
            try:
                ingested_attachments = await ingest_service.ingest_whatsapp_media_files(
                    media_files,
                    access_token=access_token,
                )
                return serialize_attachment_context(ingested_attachments)
            finally:
                await ingest_service.close()
        return []

    @staticmethod
    def _unsupported_message_text(unsupported_reason: str) -> str:
        return (
            f"I can't process WhatsApp {unsupported_reason} messages yet. "
            "Please send text, a document, an image, or an audio note."
        )

    async def _get_linked_user(
        self,
        recipient_wa_id: str,
        phone_number_id: str,
        *,
        inbound_message_id: str | None = None,
    ) -> tuple[User | None, dict[str, Any] | None]:
        # Identity lookups only trust the org mapping stored in master DB.
        # External IDs (WA IDs/phone IDs) are used only as lookup keys.
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.WHATSAPP,
                    recipient_wa_id,
                    phone_number_id,
                )
                if user and mapping:
                    now = datetime.now(UTC)
                    context = dict(mapping.conversation_context or {})
                    context.update(
                        {
                            "phone_number_id": phone_number_id,
                            "recipient_wa_id": recipient_wa_id,
                            "last_inbound_at": now.isoformat(),
                        }
                    )
                    if inbound_message_id:
                        context["last_inbound_message_id"] = inbound_message_id
                    mapping.conversation_context = context
                    mapping.last_used_at = now
                    await session.commit()
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

    async def _create_bot_job(
        self,
        user: User,
        message_text: str,
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> UUID:
        # Bot jobs and attachment links are always persisted in the user's org.
        from sqlmodel import col, select
        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
            ChatMessageAttachmentLink,
            ChatMessageRole,
            ChatMessageType,
        )
        from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import acquire_org_async_session

        org_id = user.organisation_id
        session_id: UUID | None = None
        message_id: UUID | None = None
        attachment_refs = attachment_refs or []

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
                await db.flush()

                for index, attachment_ref in enumerate(attachment_refs):
                    raw_attachment_id = attachment_ref.get("attachment_id")
                    if not raw_attachment_id:
                        continue
                    try:
                        attachment_id = UUID(str(raw_attachment_id))
                    except Exception:
                        logger.warning(
                            "Skipping invalid WhatsApp attachment ref %s",
                            raw_attachment_id,
                        )
                        continue
                    db.add(
                        ChatMessageAttachmentLink(
                            message_id=msg.message_id,
                            attachment_id=attachment_id,
                            attachment_order=index,
                        )
                    )

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
                "Created WhatsApp bot job=%s message=%s session=%s attachments=%d",
                job.job_id,
                message_id,
                session_id,
                len(attachment_refs),
            )
            return job.job_id

        raise RuntimeError("Failed to create WhatsApp bot job")

    async def send_text_message_with_id(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> str | None:
        """Send a text message and return WhatsApp message id when available."""
        response_payload = await self._send_text_message_payload(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            text=text,
        )
        if not response_payload:
            return None

        messages = response_payload.get("messages") or []
        if not messages:
            return None
        return str(messages[0].get("id") or "") or None

    async def send_text_message(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> bool:
        """Send a text message and return whether WhatsApp acknowledged success."""
        return (
            await self._send_text_message_payload(
                phone_number_id=phone_number_id,
                recipient_wa_id=recipient_wa_id,
                text=text,
            )
            is not None
        )

    async def send_typing_indicator(self, phone_number_id: str, message_id: str) -> bool:
        """Best-effort typing/read indicator for inbound WhatsApp messages.

        Indicator failure is intentionally non-blocking for message routing.
        """
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
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
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
                    "WhatsApp typing indicator failed status=%s body=%s",
                    response.status,
                    await response.text(),
                )
                return False
        except Exception:
            logger.warning("Failed to send WhatsApp typing indicator", exc_info=True)
            return False

    async def _send_text_message_payload(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> dict[str, Any] | None:
        access_token = await whatsapp_config.get_access_token()
        if not access_token:
            logger.error("WhatsApp access token not available")
            return None

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
                    return cast("dict[str, Any]", await response.json())

                logger.warning(
                    "WhatsApp send failed status=%s body=%s",
                    response.status,
                    await response.text(),
                )
                return None
        except Exception:
            logger.warning("Failed to send WhatsApp message", exc_info=True)
            return None
