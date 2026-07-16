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

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import aiohttp
from sqlalchemy.exc import IntegrityError
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.config.configs import whatsapp_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.metrics import (
    record_whatsapp_durable_job,
    record_whatsapp_enqueue,
    record_whatsapp_inbound,
)
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)

WHATSAPP_GRAPH_API_BASE = "https://graph.facebook.com"
WHATSAPP_MAX_ENQUEUE_ATTEMPTS = 5
_WHATSAPP_QUEUE_PUBLISH_TIMEOUT_SECONDS = 4
_WHATSAPP_SEND_TIMEOUT_SECONDS = 30
_WHATSAPP_TYPING_TIMEOUT_SECONDS = 3
_PERSISTED_ERROR_LIMIT = 1000


class WhatsAppInboundDurabilityError(RuntimeError):
    """The webhook could not durably retain a linked inbound message."""


@dataclass(frozen=True, slots=True)
class WhatsAppSendResult:
    """Classified Meta response used by durable outbound recovery."""

    accepted: bool
    provider_message_id: str | None = None
    failure_kind: str | None = None
    status_code: int | None = None
    error_message: str | None = None
    response_payload: dict[str, Any] | None = None


async def enqueue_whatsapp_job(*, job_id: UUID, envelope: dict[str, Any]) -> str:
    """Publish the immutable dispatch envelope used by intake and recovery."""
    from src.workers.chat_worker.client import send_chat_task_async

    return await asyncio.wait_for(
        send_chat_task_async(
            "chat.process_bot_message",
            args=[
                envelope["organisation_id"],
                envelope["user_id"],
                envelope["message_text"],
                "whatsapp",
                envelope["reply_context"],
            ],
            kwargs={
                "user_email": envelope.get("user_email"),
                "bot_context": envelope["bot_context"],
                "job_id": str(job_id),
            },
            queue="chat_messages",
            routing_key="chat_messages",
        ),
        timeout=_WHATSAPP_QUEUE_PUBLISH_TIMEOUT_SECONDS,
    )


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
            record_whatsapp_inbound("invalid")
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
                        # Voice notes use the same blob-backed attachment boundary as files;
                        # a delayed worker must not depend on Meta's temporary media URL.
                        audio_info = message.get(message_type) or message.get("audio") or {}
                        media_id = str(audio_info.get("id") or "")
                        if not media_id:
                            continue
                        mimetype = str(audio_info.get("mime_type") or "")
                        media_files = [
                            WhatsAppInboundMedia(
                                media_id=media_id,
                                media_type="audio",
                                mimetype=mimetype,
                                filename=self._whatsapp_audio_filename(mimetype),
                                message_id=message_id,
                            )
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
        """Format WhatsApp location as readable text with a clickable map target."""
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
        # Meta sends coordinates rather than a share URL. Supplying a canonical
        # link lets CRM notes retain a location people can recognize and reopen.
        maps_query = urlencode({"api": "1", "query": f"{latitude},{longitude}"})
        lines.append(f"Google Maps: https://www.google.com/maps/search/?{maps_query}")
        return "\n".join(lines)

    async def _route_message(self, message: WhatsAppInboundMessage) -> None:
        """Route one normalized message to login, job creation, and enqueue path."""
        user, _ = await self._get_linked_user(
            message.recipient_wa_id,
            message.phone_number_id,
            inbound_message_id=message.message_id,
        )
        if not user:
            record_whatsapp_inbound("unlinked")
            login_message = self._get_login_message_with_state(message)
            # Unlinked numbers cannot be trusted for chat history, so we force a
            # re-auth flow before any worker enqueue.
            await self.send_text_message(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                text=login_message,
            )
            return

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

        media_files = list(message.media_files or [])
        for audio in message.audio_files or []:
            media_id = str(audio.get("media_id") or "")
            if media_id:
                media_files.append(
                    WhatsAppInboundMedia(
                        media_id=media_id,
                        media_type="audio",
                        mimetype=str(audio.get("mimetype") or ""),
                        filename=str(audio.get("filename") or "whatsapp-audio.ogg"),
                        message_id=str(audio.get("message_id") or message.message_id),
                    )
                )

        attachment_refs = await self._ingest_media_files(user, media_files) if media_files else []
        dispatch_envelope = self._build_dispatch_envelope(user, message, attachment_refs)
        try:
            job = await self._create_or_get_durable_job(
                user=user,
                message=message,
                attachment_refs=attachment_refs,
                dispatch_envelope=dispatch_envelope,
            )
        except WhatsAppInboundDurabilityError:
            raise
        except Exception as exc:
            raise WhatsAppInboundDurabilityError("Failed to persist WhatsApp inbound job") from exc

        # Queue publication happens only after commit. Failure is recorded on the
        # pending job, which lets the scheduled recovery sweep reconstruct it.
        await self._enqueue_durable_job(user.organisation_id, job.job_id)

        try:
            # This UX-only provider call must happen after durable commit and publish.
            await self.send_typing_indicator(
                phone_number_id=message.phone_number_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.warning("Failed to send WhatsApp typing indicator", exc_info=True)

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

    def _build_dispatch_envelope(
        self,
        user: User,
        message: WhatsAppInboundMessage,
        attachment_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        bot_context: dict[str, Any] = {
            "conversation_type": "personal",
            "external_user_id": message.recipient_wa_id,
            "external_tenant_id": message.phone_number_id,
            "current_datetime": datetime.now(UTC).isoformat(),
            "whatsapp_message_id": message.message_id,
            "current_attachments": attachment_refs,
        }
        if getattr(user, "timezone", None):
            bot_context["user_timezone"] = str(user.timezone)

        return {
            "organisation_id": str(user.organisation_id),
            "user_id": str(user.user_id),
            "message_text": message.text,
            "provider": "whatsapp",
            "reply_context": self._build_reply_context(
                phone_number_id=message.phone_number_id,
                recipient_wa_id=message.recipient_wa_id,
                incoming_message_id=message.message_id,
            ),
            "bot_context": bot_context,
            "user_email": user.email,
        }

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
            raise WhatsAppInboundDurabilityError("WhatsApp media access token unavailable")

        try:
            async for db in acquire_org_async_session(user.organisation_id):
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
                    if len(ingested_attachments) != len(media_files):
                        raise WhatsAppInboundDurabilityError(
                            "WhatsApp media ingestion returned an incomplete result"
                        )
                    return serialize_attachment_context(ingested_attachments)
                finally:
                    await ingest_service.close()
        except WhatsAppInboundDurabilityError:
            raise
        except Exception as exc:
            raise WhatsAppInboundDurabilityError("WhatsApp media ingestion failed") from exc

        raise WhatsAppInboundDurabilityError("No organization session for WhatsApp media")

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
        except Exception as exc:
            logger.error("Failed to look up WhatsApp identity", exc_info=True)
            raise WhatsAppInboundDurabilityError("WhatsApp identity lookup failed") from exc

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

    @staticmethod
    def _idempotency_key(message: WhatsAppInboundMessage) -> str:
        return f"whatsapp:{message.phone_number_id}:{message.recipient_wa_id}:{message.message_id}"

    async def _create_or_get_durable_job(
        self,
        *,
        user: User,
        message: WhatsAppInboundMessage,
        attachment_refs: list[dict[str, Any]],
        dispatch_envelope: dict[str, Any],
    ):
        """Commit the inbound message, links, and reconstructable job together."""
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

        key = self._idempotency_key(message)
        job_query = select(ChatMessageJob).where(
            col(ChatMessageJob.chat_type) == ChatType.WHATSAPP_CHAT.value,
            col(ChatMessageJob.idempotency_key) == key,
        )

        async for db in acquire_org_async_session(user.organisation_id):
            try:
                existing = (await db.execute(job_query)).scalars().first()
                if existing:
                    record_whatsapp_inbound("duplicate")
                    record_whatsapp_durable_job("reused")
                    return existing

                session_query = select(ChatSession).where(
                    col(ChatSession.user_id) == user.user_id,
                    col(ChatSession.organisation_id) == user.organisation_id,
                    col(ChatSession.chat_type) == ChatType.WHATSAPP_CHAT,
                    col(ChatSession.is_deleted).is_(False),
                )
                chat_session = (await db.execute(session_query)).scalars().first()
                if not chat_session:
                    chat_session = ChatSession(
                        user_id=user.user_id,
                        organisation_id=user.organisation_id,
                        chat_type=ChatType.WHATSAPP_CHAT,
                        title="WhatsApp Chat",
                    )
                    db.add(chat_session)

                inbound_message = ChatMessage(
                    session_id=chat_session.session_id,
                    role=ChatMessageRole.USER,
                    message_type=ChatMessageType.GENERAL_MESSAGE,
                    content={"text": message.text},
                )
                db.add(inbound_message)
                await db.flush()
                chat_session.updated_at = inbound_message.created_at

                for index, attachment_ref in enumerate(attachment_refs):
                    try:
                        attachment_id = UUID(str(attachment_ref["attachment_id"]))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise WhatsAppInboundDurabilityError(
                            "Invalid durable WhatsApp attachment reference"
                        ) from exc
                    db.add(
                        ChatMessageAttachmentLink(
                            message_id=inbound_message.message_id,
                            attachment_id=attachment_id,
                            attachment_order=index,
                        )
                    )

                job = ChatMessageJob(
                    message_id=inbound_message.message_id,
                    job_type="whatsapp_chat_response",
                    chat_type=ChatType.WHATSAPP_CHAT.value,
                    status=JobStatus.PENDING,
                    user_id=user.user_id,
                    idempotency_key=key,
                    job_metadata={
                        "source": "bot",
                        "provider": "whatsapp",
                        "conversation_type": "personal",
                        "session_id": str(chat_session.session_id),
                        "whatsapp_message_id": message.message_id,
                        # Keep the metadata copy while deployed workers and operator
                        # queries transition to the normalized column.
                        "idempotency_key": key,
                        "phone_number_id": message.phone_number_id,
                        "recipient_wa_id": message.recipient_wa_id,
                        "dispatch_envelope": dispatch_envelope,
                        "enqueue_status": "pending",
                        "enqueue_attempts": 0,
                        "celery_task_id": None,
                        "last_enqueue_error": None,
                        "processing_stage": "pending",
                        "processing_attempts": 0,
                        "send_status": "not_started",
                        "send_attempts": 0,
                        "next_send_attempt_at": None,
                        "outbound_chunks": [],
                    },
                )
                db.add(job)
                await db.commit()
                record_whatsapp_inbound("new")
                record_whatsapp_durable_job("created")
                logger.info(
                    "Created durable WhatsApp job=%s message=%s",
                    job.job_id,
                    inbound_message.message_id,
                )
                return job
            except IntegrityError as exc:
                await db.rollback()
                winner = await self._load_durable_job_by_key(user.organisation_id, key)
                if winner:
                    record_whatsapp_inbound("duplicate")
                    record_whatsapp_durable_job("reused")
                    return winner
                record_whatsapp_durable_job("failed")
                raise WhatsAppInboundDurabilityError(
                    "WhatsApp idempotency conflict had no winning job"
                ) from exc
            except WhatsAppInboundDurabilityError:
                await db.rollback()
                record_whatsapp_durable_job("failed")
                raise
            except Exception as exc:
                await db.rollback()
                record_whatsapp_durable_job("failed")
                raise WhatsAppInboundDurabilityError(
                    "Failed to commit durable WhatsApp job"
                ) from exc

        raise WhatsAppInboundDurabilityError("No organization session for WhatsApp job")

    async def _load_durable_job_by_key(self, organisation_id: UUID, key: str):
        from sqlmodel import col, select
        from src.domain.models.organisation_tables.chat_session_models import ChatType
        from src.domain.models.organisation_tables.job_models import ChatMessageJob
        from src.infra.database.sessions import acquire_org_async_session

        async for db in acquire_org_async_session(organisation_id):
            statement = select(ChatMessageJob).where(
                col(ChatMessageJob.chat_type) == ChatType.WHATSAPP_CHAT.value,
                col(ChatMessageJob.idempotency_key) == key,
            )
            return (await db.execute(statement)).scalars().first()
        return None

    async def _enqueue_durable_job(self, organisation_id: UUID, job_id: UUID) -> bool:
        try:
            claim = await self._claim_job_for_enqueue(organisation_id, job_id)
            if not claim:
                return False
            envelope, attempt = claim
            try:
                task_id = await enqueue_whatsapp_job(job_id=job_id, envelope=envelope)
            except Exception as exc:
                await self._record_enqueue_result(
                    organisation_id,
                    job_id,
                    attempt=attempt,
                    task_id=None,
                    error=str(exc)[:_PERSISTED_ERROR_LIMIT],
                )
                logger.error("Failed to enqueue durable WhatsApp job=%s", job_id, exc_info=True)
                record_whatsapp_enqueue("failed")
                return False

            await self._record_enqueue_result(
                organisation_id,
                job_id,
                attempt=attempt,
                task_id=task_id,
                error=None,
            )
            record_whatsapp_enqueue("enqueued")
            return True
        except Exception:
            # The job already exists. A later database sweep owns recovery if this
            # metadata or broker operation cannot complete in the webhook request.
            logger.error("Failed to publish durable WhatsApp job=%s", job_id, exc_info=True)
            record_whatsapp_enqueue("failed")
            return False

    async def _claim_job_for_enqueue(
        self, organisation_id: UUID, job_id: UUID
    ) -> tuple[dict[str, Any], int] | None:
        from sqlmodel import select
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import acquire_org_async_session

        async for db in acquire_org_async_session(organisation_id):
            statement = (
                select(ChatMessageJob).where(ChatMessageJob.job_id == job_id).with_for_update()
            )
            job = (await db.execute(statement)).scalars().first()
            if not job or getattr(job.status, "value", job.status) != JobStatus.PENDING.value:
                return None

            metadata = dict(job.job_metadata or {})
            if metadata.get("enqueue_status") not in {"pending", "failed"}:
                return None
            attempt = int(metadata.get("enqueue_attempts") or 0) + 1
            if attempt > WHATSAPP_MAX_ENQUEUE_ATTEMPTS:
                job.status = JobStatus.FAILED
                job.error_message = "WhatsApp enqueue retry budget exhausted"
                metadata["enqueue_status"] = "exhausted"
                job.job_metadata = metadata
                db.add(job)
                await db.commit()
                record_whatsapp_enqueue("exhausted")
                return None

            envelope = metadata.get("dispatch_envelope")
            if not isinstance(envelope, dict):
                job.status = JobStatus.FAILED
                job.error_message = "WhatsApp dispatch envelope is missing"
                metadata["enqueue_status"] = "invalid"
                job.job_metadata = metadata
                db.add(job)
                await db.commit()
                return None

            metadata.update(
                {
                    "enqueue_status": "enqueuing",
                    "enqueue_attempts": attempt,
                    "last_enqueue_error": None,
                }
            )
            job.job_metadata = metadata
            db.add(job)
            await db.commit()
            return dict(envelope), attempt
        return None

    async def _record_enqueue_result(
        self,
        organisation_id: UUID,
        job_id: UUID,
        *,
        attempt: int,
        task_id: str | None,
        error: str | None,
    ) -> None:
        from src.domain.models.organisation_tables.job_models import ChatMessageJob
        from src.infra.database.sessions import acquire_org_async_session

        async for db in acquire_org_async_session(organisation_id):
            job = await db.get(ChatMessageJob, job_id)
            if not job:
                return
            metadata = dict(job.job_metadata or {})
            if int(metadata.get("enqueue_attempts") or 0) != attempt:
                return
            metadata.update(
                {
                    "enqueue_status": "enqueued" if task_id else "failed",
                    "celery_task_id": task_id,
                    "last_enqueue_error": error,
                }
            )
            job.job_metadata = metadata
            db.add(job)
            await db.commit()
            return

    async def send_text_message_with_id(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> str | None:
        """Send a text message and return WhatsApp message id when available."""
        result = await self.send_text_message_result(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            text=text,
        )
        return result.provider_message_id if result.accepted else None

    async def send_text_message(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> bool:
        """Send a text message and return whether WhatsApp acknowledged success."""
        result = await self.send_text_message_result(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            text=text,
        )
        return result.accepted

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
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=_WHATSAPP_TYPING_TIMEOUT_SECONDS)
                ) as session,
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
        """Compatibility wrapper for callers that still need Meta's payload."""
        result = await self.send_text_message_result(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            text=text,
        )
        return result.response_payload if result.accepted else None

    async def send_text_message_result(
        self, phone_number_id: str, recipient_wa_id: str, text: str
    ) -> WhatsAppSendResult:
        """Send text and preserve enough provider evidence for retry decisions."""
        access_token = await whatsapp_config.get_access_token()
        if not access_token:
            logger.error("WhatsApp access token not available")
            return WhatsAppSendResult(
                accepted=False,
                failure_kind="terminal",
                error_message="WhatsApp access token unavailable",
            )

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
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=_WHATSAPP_SEND_TIMEOUT_SECONDS)
                ) as session,
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
                    response_payload = await response.json()
                    messages = response_payload.get("messages") or []
                    provider_message_id = str(messages[0].get("id") or "") if messages else ""
                    if provider_message_id:
                        return WhatsAppSendResult(
                            accepted=True,
                            provider_message_id=provider_message_id,
                            status_code=response.status,
                            response_payload=response_payload,
                        )
                    return WhatsAppSendResult(
                        accepted=False,
                        failure_kind="ambiguous",
                        status_code=response.status,
                        error_message="Meta returned success without a message ID",
                        response_payload=response_payload,
                    )

                response_body = (await response.text())[:_PERSISTED_ERROR_LIMIT]
                logger.warning(
                    "WhatsApp send failed status=%s body=%s",
                    response.status,
                    response_body,
                )
                return WhatsAppSendResult(
                    accepted=False,
                    failure_kind=(
                        "retryable"
                        if response.status == 429 or response.status >= 500
                        else "terminal"
                    ),
                    status_code=response.status,
                    error_message=response_body or f"Meta returned HTTP {response.status}",
                )
        except Exception as exc:
            logger.warning("Failed to send WhatsApp message", exc_info=True)
            return WhatsAppSendResult(
                accepted=False,
                failure_kind="ambiguous",
                error_message=str(exc)[:_PERSISTED_ERROR_LIMIT],
            )
