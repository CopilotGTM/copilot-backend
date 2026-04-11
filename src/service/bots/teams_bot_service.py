"""Microsoft Teams Bot Service.

Handles incoming messages from Teams via Azure Bot Service.
This service processes Bot Framework activities and sends responses.

Bot credentials are fetched from Azure Master Key Vault at runtime.

Identity Flow:
1. User sends message -> Bot checks external_identity_mappings
2. If linked -> Route to agent (TODO), update conversation context
3. If not linked -> Generate login URL with secure state token
"""

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import aiohttp
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.infra.config.configs import teams_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
from src.service.bots.auth_validators import AuthValidationResult, TeamsAuthValidator
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)


class TeamsBotService:
    """
    Handles Microsoft Teams bot interactions with identity-aware routing.

    This service:
    1. Receives activities (messages, events) from Azure Bot Service
    2. Checks identity mapping to determine if user is linked
    3. Routes authenticated users to appropriate handlers
    4. Generates login URLs with state tokens for unauthenticated users
    5. Sends responses back to Teams

    Credentials are fetched from Azure Key Vault on first use.
    """

    def __init__(self):
        self._app_id: str | None = None
        self._app_secret: str | None = None
        self._tenant_id: str | None = None
        self._credentials_loaded = False
        self._identity_service = ExternalIdentityService()
        self._auth_state_service: BotAuthStateService | None = None
        self._auth_validator: TeamsAuthValidator | None = None

    @property
    def auth_state_service(self) -> BotAuthStateService:
        """Lazy-load the auth state service."""
        if self._auth_state_service is None:
            self._auth_state_service = get_bot_auth_state_service()
        return self._auth_state_service

    async def _ensure_credentials(self) -> bool:
        """Load bot credentials from TeamsConfig (TTL-cached Key Vault)."""
        if self._credentials_loaded:
            return bool(self._app_id and self._app_secret)

        self._app_id, self._app_secret, self._tenant_id = await teams_config.get_bot_credentials()
        self._credentials_loaded = True

        if not self._app_id or not self._app_secret:
            logger.error("Teams bot credentials not available")
            return False

        return True

    async def validate_auth_header(self, auth_header: str) -> AuthValidationResult:
        """Validate the Bot Framework Authorization header."""
        # Ensure credentials are loaded
        if not await self._ensure_credentials():
            return AuthValidationResult(False, "credentials_unavailable")

        # Validate using TeamsAuthValidator
        if not self._app_id:
            return AuthValidationResult(False, "app_id_missing")

        # Lazy-load the auth validator
        if self._auth_validator is None:
            self._auth_validator = TeamsAuthValidator(self._app_id)

        return await self._auth_validator.validate({"Authorization": auth_header})

    async def process_activity(self, activity: dict[str, Any]) -> dict[str, Any] | None:
        """
        Process incoming activity from Teams.

        Args:
            activity: The Bot Framework activity (message, event, etc.)

        Returns:
            For invoke activities: response dict to return to Teams. None otherwise.
        """
        activity_type = activity.get("type", "")

        if activity_type == "message":
            await self._handle_message(activity)
            return None
        elif activity_type == "conversationUpdate":
            await self._handle_conversation_update(activity)
            return None
        elif activity_type == "invoke":
            return await self._handle_invoke(activity)
        else:
            logger.debug("Unhandled activity type: %s", activity_type)
            return None

    async def _handle_message(self, activity: dict[str, Any]) -> None:
        """Handle incoming message from user with identity-aware routing."""
        # Detect conversation type (personal vs group/channel/meeting) first
        conversation = activity.get("conversation", {})
        conversation_type = conversation.get("conversationType", "")
        conversation_id = conversation.get("id", "")
        service_url = activity.get("serviceUrl", "")

        # Common fields used in all branches
        from_user = activity.get("from", {})
        teams_user_id = from_user.get("id", "")
        teams_user_name = from_user.get("name", "")
        user_aad_object_id = from_user.get("aadObjectId", "")
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        # Verifying User Identity
        user, _ = await self._get_linked_user(teams_user_id, teams_tenant_id)
        if not user:
            login_message = self._get_login_message_with_state(
                teams_user_id,
                teams_tenant_id,
                teams_user_name,
                service_url,
                conversation_id,
                user_aad_object_id=user_aad_object_id,
            )
            await self._send_reply(activity, login_message, service_url)
            return

        # Check for Adaptive Card form submissions (Action.Submit sends as
        # type=message with form data in "value" and no "text")
        form_data = activity.get("value")
        if isinstance(form_data, dict) and form_data.get("action"):
            await self._handle_form_submission(activity, form_data, service_url)
            return

        text: str = activity.get("text") or ""

        # Extract file attachments (ignore Adaptive Cards and other non-file attachments).
        # File uploads arrive as "application/vnd.microsoft.teams.file.download.info",
        # inline/pasted images arrive as "image/*".
        raw_attachments = activity.get("attachments", [])
        logger.info(
            "Teams raw attachments (%d): %s",
            len(raw_attachments),
            [
                {"contentType": a.get("contentType"), "name": a.get("name", "?")}
                for a in raw_attachments
            ],
        )
        teams_files = self._extract_file_attachments(raw_attachments)

        if not text.strip() and not teams_files:
            logger.info("Ignoring empty message from user %s", user.email)
            await self._send_reply(activity, "I received an empty message.", service_url)
            return

        if teams_files:
            logger.info(
                "Teams message from %s includes %d file attachment(s)",
                user.email,
                len(teams_files),
            )

        # Teams sends the user's IANA timezone (e.g. "Asia/Kolkata") on the activity
        local_timezone = activity.get("localTimezone", "")

        if conversation_type == "personal":
            text = activity.get("text") or ""
            response = await self._handle_authenticated_message(
                text,
                user,
                service_url,
                conversation_id,
                conversation_type,
                user_aad_object_id=user_aad_object_id,
                external_user_id=teams_user_id,
                external_tenant_id=teams_tenant_id,
                local_timezone=local_timezone,
                teams_files=teams_files or None,
            )

        elif conversation_type in ["groupChat", "channel", "meeting"]:
            response = await self._handle_authenticated_message(
                text,
                user,
                service_url,
                conversation_id,
                conversation_type,
                user_aad_object_id=user_aad_object_id,
                external_user_id=teams_user_id,
                external_tenant_id=teams_tenant_id,
                local_timezone=local_timezone,
                teams_files=teams_files or None,
            )
        else:
            logger.warning("Teams unexpected conversationType: %s", conversation_type)
            await self._send_reply(
                activity,
                "Invalid message format. Unable to respond.",
                service_url,
            )
            return

        # Send error response back to Teams (typing indicator already sent on success)
        if response:
            await self._send_reply(activity, response, service_url)

    async def _get_linked_user(
        self, teams_user_id: str, teams_tenant_id: str
    ) -> tuple[User | None, dict[str, Any] | None]:
        """Look up linked CopilotGTM user for Teams identity.

        Returns:
            Tuple of (User, conversation_context) if found, (None, None) otherwise.
        """
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.TEAMS,
                    teams_user_id,
                    teams_tenant_id,
                )
                if user and mapping:
                    return user, mapping.conversation_context
                return None, None
            return None, None
        except Exception as e:
            logger.error("Failed to look up Teams identity: %s", e, exc_info=True)
            return None, None

    async def _handle_authenticated_message(
        self,
        text: str,
        user: User,
        service_url: str,
        conversation_id: str,
        conversation_type: str = "personal",
        user_aad_object_id: str = "",
        external_user_id: str = "",
        external_tenant_id: str = "",
        local_timezone: str = "",
        teams_files: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Handle a message from an authenticated user.

        Enqueues a Celery task to process the message through the GeneralAgent.
        The agent response will be sent back as a proactive message.

        Sends a thinking indicator on success (returns None).
        Returns an error string only if enqueue fails.
        """
        from src.infra.database.sessions import acquire_org_async_session
        from src.service.chat.chat_attachment_manifest import serialize_attachment_context
        from src.service.chat.external_chat_attachment_ingest_service import (
            ExternalChatAttachmentIngestService,
        )
        from src.workers.chat_worker.client import send_chat_task_async

        bot_context: dict[str, Any] = {
            "conversation_type": conversation_type,
            "teams_conversation_id": conversation_id,
            "user_aad_object_id": user_aad_object_id,
            "external_user_id": external_user_id,
            "external_tenant_id": external_tenant_id,
            "current_datetime": datetime.now(UTC).isoformat(),
        }
        if local_timezone:
            bot_context["user_timezone"] = local_timezone

        attachment_refs: list[dict[str, Any]] = []
        if teams_files:
            # Ingest before enqueue so later worker execution never depends on Teams
            # activity-scoped download URLs still being valid.
            async for db in acquire_org_async_session(user.organisation_id):
                ingest_service = ExternalChatAttachmentIngestService(
                    org_session=db,
                    organisation_id=user.organisation_id,
                    user_id=user.user_id,
                )
                try:
                    ingested_attachments = await ingest_service.ingest_teams_files(
                        teams_files,
                        token_getter=self._get_teams_token,
                    )
                    attachment_refs = serialize_attachment_context(ingested_attachments)
                finally:
                    await ingest_service.close()
                break

            if attachment_refs:
                bot_context["current_attachments"] = attachment_refs

        job_id: str | None = None
        try:
            job_id = str(
                await self._create_bot_job(
                    user=user,
                    message_text=text,
                    conversation_type=conversation_type,
                    attachment_refs=attachment_refs,
                )
            )
        except Exception:
            logger.exception("Failed to create bot chat job, dispatching without job_id")

        try:
            task_id = await send_chat_task_async(
                "chat.process_bot_message",
                args=[
                    str(user.organisation_id),
                    str(user.user_id),
                    text,
                    "teams",
                    {"service_url": service_url, "conversation_id": conversation_id},
                ],
                kwargs={
                    "user_email": user.email,
                    "bot_context": bot_context,
                    "job_id": job_id,
                },
                queue="chat_messages",
            )
            logger.info(
                "Enqueued bot message task=%s job=%s for user=%s org=%s type=%s",
                task_id,
                job_id,
                user.email,
                user.organisation_id,
                conversation_type,
            )
        except Exception as e:
            logger.error("Failed to enqueue bot message task: %s", e, exc_info=True)
            return (
                "I'm sorry, I'm having trouble processing your request right now. "
                "Please try again in a moment."
            )

        thinking_activity_id = await self.send_thinking_message(
            service_url, conversation_id, "Thinking..."
        )
        if thinking_activity_id:
            try:
                from src.infra.messaging.redis_client import get_redis_client

                redis = get_redis_client()
                redis.set(
                    f"teams_thinking_conv:{conversation_id}",
                    thinking_activity_id,
                    ex=300,
                )
            except Exception:
                logger.debug("Failed to cache Teams thinking activity ID", exc_info=True)
        else:
            await self._send_typing(service_url, conversation_id)
        return None

    async def _create_bot_job(
        self,
        user: User,
        message_text: str,
        conversation_type: str,
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> UUID:
        """Create a PENDING ChatMessageJob before dispatching the Celery task.

        For personal chats: creates session + user message + attachment links + job.
        For group/channel chats: creates job only (message_id=None).
        """
        from sqlalchemy.exc import IntegrityError
        from sqlmodel import col, select
        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
            ChatMessageAttachmentLink,
            ChatMessageRole,
            ChatMessageType,
        )
        from src.domain.models.organisation_tables.chat_session_models import (
            ChatSession,
            ChatType,
        )
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import acquire_org_async_session

        org_id = user.organisation_id
        message_id: UUID | None = None
        session_id: UUID | None = None
        attachment_refs = attachment_refs or []

        if conversation_type == "personal":
            async for db in acquire_org_async_session(org_id):
                stmt = select(ChatSession).where(
                    col(ChatSession.user_id) == user.user_id,
                    col(ChatSession.organisation_id) == org_id,
                    col(ChatSession.chat_type) == ChatType.TEAMS_CHAT,
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
                            "chat_type": ChatType.TEAMS_CHAT,
                            "title": "Teams Chat",
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
                # Only persisted personal chats get attachment links. Group and channel
                # messages keep transient attachment access through bot_context.
                async for db in acquire_org_async_session(org_id):
                    msg = ChatMessage(
                        session_id=session_id,
                        role=ChatMessageRole.USER,
                        message_type=ChatMessageType.GENERAL_MESSAGE,
                        content={"text": message_text},
                    )
                    db.add(msg)
                    await db.flush()

                    # Preserve attachment order so the manifest mirrors the UI order the user saw.
                    for index, attachment_ref in enumerate(attachment_refs):
                        raw_attachment_id = attachment_ref.get("attachment_id")
                        if not raw_attachment_id:
                            continue
                        try:
                            attachment_id = UUID(str(raw_attachment_id))
                        except Exception:
                            logger.warning(
                                "Skipping invalid Teams attachment ref %s",
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
                job_type="teams_chat_response",
                chat_type="TEAMS_CHAT",
                status=JobStatus.PENDING,
                user_id=user.user_id,
                job_metadata={
                    "source": "bot",
                    "provider": "teams",
                    "conversation_type": conversation_type,
                    **({"session_id": str(session_id)} if session_id else {}),
                },
            )
            db.add(job)
            await db.commit()
            logger.info(
                "Created Teams bot job=%s message=%s session=%s type=%s attachments=%d",
                job.job_id,
                message_id,
                session_id,
                conversation_type,
                len(attachment_refs),
            )
            return job.job_id

        raise RuntimeError("Failed to create Teams bot job")

    async def _handle_conversation_update(self, activity: dict[str, Any]) -> None:
        """Handle conversation update (user added/removed, bot installed)."""
        members_added = activity.get("membersAdded", [])
        recipient_id = activity.get("recipient", {}).get("id", "")
        service_url = activity.get("serviceUrl", "")

        for member in members_added:
            if member.get("id") != recipient_id:
                # New user added - send welcome message
                logger.info(
                    "New user added to Teams conversation: %s", member.get("name", "unknown")
                )
                await self._send_reply(activity, self._get_welcome_message(), service_url)
                break

    async def _handle_form_submission(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
        service_url: str,
    ) -> None:
        """Handle Adaptive Card Action.Submit form submissions.

        Teams sends Action.Submit as type=message with form data in "value".
        This method routes the submission to the appropriate form handler.
        """
        from src.service.bots.form_handlers import FormHandlerRegistry

        action = form_data.get("action", "")
        logger.info("Form submission received: action=%s", action)

        result = await FormHandlerRegistry.handle(action, activity, form_data)
        if result and result.card_payload:
            conversation = activity.get("conversation", {})
            conversation_id = conversation.get("id", "")
            await self.send_proactive_payload(
                service_url=service_url,
                conversation_id=conversation_id,
                payload={
                    "text": " ",
                    "attachments": [
                        {
                            "contentType": "application/vnd.microsoft.card.adaptive",
                            "content": result.card_payload,
                        }
                    ],
                },
                text_fallback=result.message or "Form update",
            )
        elif result:
            await self._send_reply(activity, result.message, service_url)
        else:
            logger.warning("No handler for form action: %s", action)
            await self._send_reply(
                activity,
                "Sorry, I couldn't process that form submission.",
                service_url,
            )

    async def _handle_invoke(self, activity: dict[str, Any]) -> dict[str, Any] | None:
        """Handle invoke activities (adaptive card actions, form submissions, etc.).

        Routes Action.Execute invokes (adaptiveCard/action) to card-replacement flow,
        and falls back to legacy handling for other invoke types.

        Returns:
            Response dict for Teams (invoke activities require a response body),
            or None if the invoke is unhandled.
        """
        invoke_name = activity.get("name", "")
        logger.debug("Invoke activity received: name=%s", invoke_name)

        # Action.Execute sends invoke with name="adaptiveCard/action"
        if invoke_name == "adaptiveCard/action":
            return await self._handle_adaptive_card_action(activity)

        # Legacy: Action.Submit that arrives as invoke (value.action)
        from src.service.bots.form_handlers import FormHandlerRegistry

        form_data = activity.get("value", {})
        action = form_data.get("action", "")

        if action:
            service_url = activity.get("serviceUrl", "")

            result = await FormHandlerRegistry.handle(action, activity, form_data)
            if result:
                await self._send_reply(activity, result.message, service_url)
                return {
                    "status": 200,
                    "body": {"type": "message", "text": "OK"},
                }

        logger.debug("Unhandled invoke: name=%s action=%s", invoke_name, action)
        return None

    async def _handle_adaptive_card_action(self, activity: dict[str, Any]) -> dict[str, Any]:
        """Handle Action.Execute invoke — process form and return replacement card.

        Action.Execute sends:
            activity.value.action.verb = "<action_name>"
            activity.value.action.data = {input field values + metadata}

        Returns an invoke response with a replacement Adaptive Card that Teams
        swaps in-place (no buttons = no re-submission).
        """
        from src.adapters.card_builders.teams_card_builder import (
            build_confirmation_card,
            build_error_card,
        )
        from src.service.bots.form_handlers import FormHandlerRegistry

        value = activity.get("value", {})
        action_payload = value.get("action", {})
        verb = action_payload.get("verb", "")
        form_data = action_payload.get("data", {})

        logger.info("Adaptive card Action.Execute: verb=%s", verb)

        if not verb:
            return {
                "statusCode": 200,
                "type": "application/vnd.microsoft.activity.message",
                "value": "Unknown action.",
            }

        result = await FormHandlerRegistry.handle(verb, activity, form_data)

        if result and result.card_payload:
            return {
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": result.card_payload,
            }
        elif result and result.success:
            return {
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": build_confirmation_card(result.message),
            }
        elif result:
            return {
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": build_error_card(result.message),
            }
        else:
            logger.warning("No handler for Action.Execute verb: %s", verb)
            return {
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": build_error_card("Sorry, I couldn't process that form submission."),
            }

    async def _get_teams_token(
        self, scope: str = "https://api.botframework.com/.default"
    ) -> str | None:
        """Get an Azure AD token for Teams file downloads during API-side ingestion.

        The attachment ingest service needs a scope-aware token getter because inline
        images use Bot Framework auth while OneDrive/SharePoint files use Graph auth.
        """
        if not await self._ensure_credentials():
            return None
        if not self._app_id or not self._app_secret:
            logger.warning("Cannot get Teams token: bot credentials unavailable")
            return None

        tenant = self._tenant_id or "botframework.com"
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_secret,
            "scope": scope,
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(token_url, data=data) as response,
            ):
                if response.status == 200:
                    result = await response.json()
                    access_token = result.get("access_token")
                    return access_token if isinstance(access_token, str) else None

                response_text = await response.text()
                logger.warning(
                    "Failed to get Teams token (scope=%s): status=%s response=%s",
                    scope,
                    response.status,
                    response_text[:200],
                )
                return None
        except Exception:
            logger.warning("Failed to get Teams token for scope=%s", scope, exc_info=True)
            return None

    @staticmethod
    def _extract_file_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract downloadable file metadata from Teams activity attachments.

        Three attachment types are handled:
        1. Local file uploads: contentType "application/vnd.microsoft.teams.file.download.info"
           → pre-authenticated downloadUrl (no auth needed).
        2. OneDrive/SharePoint shares: contentType "application/vnd.microsoft.teams.card.file.info"
           → contentUrl points to SharePoint; requires Graph API token to download.
        3. Inline/pasted images: contentType "image/*"
           → contentUrl requires bot access token.

        Adaptive Cards, text/html, and other non-file attachments are filtered out.
        """
        file_download_ct = "application/vnd.microsoft.teams.file.download.info"
        file_info_ct = "application/vnd.microsoft.teams.card.file.info"
        files: list[dict[str, Any]] = []

        for att in attachments:
            ct = (att.get("contentType") or "").lower()

            if ct == file_download_ct:
                # Local file upload → pre-authenticated download URL
                content = att.get("content") or {}
                download_url = content.get("downloadUrl", "")
                if download_url:
                    files.append(
                        {
                            "name": att.get("name", "file"),
                            "download_url": download_url,
                            "content_url": att.get("contentUrl", ""),
                            "file_type": content.get("fileType", ""),
                            "source": "upload",
                            "auth_required": False,
                        }
                    )

            elif ct == file_info_ct:
                # OneDrive/SharePoint file share → requires Graph API
                content_url = att.get("contentUrl", "")
                content = att.get("content") or {}
                if content_url:
                    files.append(
                        {
                            "name": att.get("name", "file"),
                            "download_url": content_url,
                            "content_url": content_url,
                            "file_type": content.get("fileType", ""),
                            "source": "onedrive",
                            "auth_required": True,
                        }
                    )

            elif ct.startswith("image/"):
                # Inline/pasted image → requires bot access token
                content_url = att.get("contentUrl", "")
                if content_url:
                    files.append(
                        {
                            "name": att.get("name", "image"),
                            "download_url": content_url,
                            "content_url": content_url,
                            "mimetype": ct,
                            "source": "inline",
                            "auth_required": True,
                        }
                    )

        return files

    def _get_welcome_message(self) -> str:
        """Generate welcome message for new users."""
        return (
            "**Welcome to CopilotGTM!**\n\n"
            "I'm your AI-powered sales assistant. I can help you find answers from "
            "your organization's documentation and deal information.\n\n"
            "**To get started:**\n"
            "1. Type **login** to sign in to your account\n"
            "2. Once signed in, just ask me any question!\n\n"
            "Type **help** to see all available commands."
        )

    def _get_login_message_with_state(
        self,
        teams_user_id: str,
        teams_tenant_id: str,
        teams_user_name: str | None,
        service_url: str,
        conversation_id: str,
        user_aad_object_id: str = "",
    ) -> str:
        """Generate login prompt with secure state token."""
        if not teams_config.TEAMS_AUTH_LOGIN_URL:
            return (
                "**Sign In Required**\n\n"
                "Please sign in to your CopilotGTM account to use this bot.\n"
                "Contact your administrator to get the login URL."
            )

        # Create state token with Teams-specific context
        state = self.auth_state_service.create_state(
            provider=ExternalIdentityProvider.TEAMS,
            external_user_id=teams_user_id,
            external_tenant_id=teams_tenant_id,
            external_user_name=teams_user_name,
            context={
                "service_url": service_url,
                "conversation_id": conversation_id,
                "user_aad_object_id": user_aad_object_id,
            },
        )

        # Generate login URL with state
        login_url = self.auth_state_service.generate_login_url(state)

        if not login_url:
            return (
                "**Sign In Required**\n\n"
                "Unable to generate login link. Please contact your administrator."
            )

        return (
            "**Sign In Required**\n\n"
            f"Please sign in to your CopilotGTM account:\n\n"
            f"[Click here to sign in]({login_url})\n\n"
            "After signing in, return here and try your question again.\n\n"
            "_This link expires in 5 minutes._"
        )

    async def _send_typing(self, service_url: str, conversation_id: str) -> None:
        """Send a typing indicator to Teams.

        The typing indicator shows "..." in the chat for ~3 seconds,
        then disappears automatically. Unlike a text message, it leaves
        no permanent trace in the conversation.
        """
        if not service_url or not conversation_id:
            return

        access_token = await self._get_access_token()
        if not access_token:
            logger.warning("Cannot send typing indicator: failed to get access token")
            return

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        typing_activity: dict[str, Any] = {
            "type": "typing",
        }
        # Include bot identity if credentials are loaded
        if self._app_id:
            typing_activity["from"] = {"id": self._app_id}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=typing_activity, headers=headers) as response,
            ):
                if response.status in (200, 201, 202):
                    logger.debug("Typing indicator sent to conversation=%s", conversation_id)
                else:
                    response_text = await response.text()
                    logger.warning(
                        "Typing indicator failed: status=%s response=%s",
                        response.status,
                        response_text[:200],
                    )
        except Exception as e:
            # Non-critical — don't fail the request if typing indicator fails
            logger.warning("Failed to send typing indicator: %s", e)

    async def _send_reply(
        self,
        activity: dict[str, Any],
        text: str,
        service_url: str,
    ) -> None:
        """Send a reply message back to Teams."""
        if not service_url:
            logger.error("Cannot send reply: no service URL")
            return

        conversation_id = activity.get("conversation", {}).get("id", "")
        activity_id = activity.get("id", "")

        if not conversation_id:
            logger.error("Cannot send reply: no conversation ID")
            return

        # Get access token for Bot Framework
        access_token = await self._get_access_token()
        if not access_token:
            logger.error("Cannot send reply: failed to get access token")
            return

        # Build reply activity
        reply_url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        reply_activity = {
            "type": "message",
            "from": activity.get("recipient"),
            "conversation": activity.get("conversation"),
            "recipient": activity.get("from"),
            "text": text,
            "replyToId": activity_id,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    reply_url,
                    json=reply_activity,
                    headers=headers,
                ) as response,
            ):
                if response.status in (200, 201):
                    logger.info("Teams reply sent successfully")
                else:
                    response_text = await response.text()
                    logger.error(
                        "Failed to send Teams reply: status=%s, response=%s",
                        response.status,
                        response_text[:200],
                    )
        except Exception as e:
            logger.error("Error sending Teams reply: %s", e, exc_info=True)

    async def _get_access_token(self) -> str | None:
        """Get access token for Bot Framework API calls."""
        if not self._app_id or not self._app_secret:
            logger.error("Bot credentials not loaded")
            return None

        # Get token from Azure AD
        # Use botframework.com for multi-tenant, or specific tenant for single-tenant
        tenant = self._tenant_id or "botframework.com"
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_secret,
            "scope": "https://api.botframework.com/.default",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(token_url, data=data) as response,
            ):
                if response.status == 200:
                    result = await response.json()
                    access_token = result.get("access_token")
                    return access_token if isinstance(access_token, str) else None
                else:
                    response_text = await response.text()
                    logger.error(
                        "Failed to get Bot Framework token: status=%s, response=%s",
                        response.status,
                        response_text[:200],
                    )
                    return None
        except Exception as e:
            logger.error("Error getting Bot Framework token: %s", e, exc_info=True)
            return None

    async def send_proactive_message(
        self,
        service_url: str,
        conversation_id: str,
        message: str,
    ) -> bool:
        """
        Send a proactive message to a Teams conversation.

        Used to send messages without a user activity trigger,
        e.g., sending async responses from Celery workers.
        """
        if not await self._ensure_credentials():
            return False

        access_token = await self._get_access_token()
        if not access_token:
            return False

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        activity = {
            "type": "message",
            "text": message,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=activity, headers=headers) as response,
            ):
                if response.status in (200, 201):
                    logger.info("Proactive message sent to Teams")
                    return True
                else:
                    response_text = await response.text()
                    logger.error(
                        "Failed to send proactive message: status=%s, response=%s",
                        response.status,
                        response_text[:200],
                    )
                    return False
        except Exception as e:
            logger.error("Error sending proactive message: %s", e, exc_info=True)
            return False

    async def send_proactive_payload(
        self,
        service_url: str,
        conversation_id: str,
        payload: dict[str, Any],
        text_fallback: str | None = None,
    ) -> bool:
        """Send a proactive message with rich payload (Adaptive Cards, etc.) to Teams.

        Args:
            service_url: Teams service URL
            conversation_id: Target conversation ID
            payload: The message payload (can include attachments for Adaptive Cards)
            text_fallback: Fallback text for channels that don't support rich UI

        Returns:
            True if sent successfully
        """
        if not await self._ensure_credentials():
            return False

        access_token = await self._get_access_token()
        if not access_token:
            return False

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        activity: dict[str, Any] = {
            "type": "message",
            "text": text_fallback or " ",
        }

        if payload:
            activity.update(payload)

        # Bot Framework rejects activities with empty text and no attachments.
        # This can happen when the AI response is truncated and all content
        # gets stripped by the streaming buffer's marker extraction.
        if not activity.get("text", "").strip() and "attachments" not in activity:
            activity["text"] = "I wasn't able to generate a complete response. Please try again."

        # Escape markdown reference link definitions (e.g. "[^1]: text", "[label]: url")
        # at the start of lines. Bot Framework strips these, leaving empty content
        # → BadSyntax error. Only targets "[x]: " patterns, not normal "[link](url)".
        if "attachments" not in activity and activity.get("text"):
            activity["text"] = re.sub(r"(?m)^(\[[^\]]*\]):\s", r"\\\1: ", activity["text"])

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=activity, headers=headers) as http_response,
            ):
                if http_response.status in (200, 201):
                    logger.info("Proactive payload sent to Teams")
                    return True
                else:
                    response_text = await http_response.text()
                    logger.error(
                        "Failed to send proactive payload: status=%s, response=%s",
                        http_response.status,
                        response_text[:200],
                    )
                    return False
        except Exception as e:
            logger.error("Error sending proactive payload: %s", e, exc_info=True)
            return False

    async def send_thinking_message(
        self,
        service_url: str,
        conversation_id: str,
        text: str = "Thinking...",
    ) -> str | None:
        """Send a thinking placeholder message and return its activity ID.

        The activity ID can be used to update this message with tool status
        and finally replace it with the full answer.
        """
        if not await self._ensure_credentials():
            return None

        access_token = await self._get_access_token()
        if not access_token:
            return None

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        activity = {
            "type": "message",
            "text": f"⏳ _{text}_",
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=activity, headers=headers) as response,
            ):
                if response.status in (200, 201):
                    result = await response.json()
                    return str(result.get("id", ""))
                else:
                    response_text = await response.text()
                    logger.warning(
                        "Failed to send Teams thinking message: status=%s, response=%s",
                        response.status,
                        response_text[:200],
                    )
        except Exception:
            logger.debug("Failed to send Teams thinking message", exc_info=True)
        return None

    async def update_activity(
        self,
        service_url: str,
        conversation_id: str,
        activity_id: str,
        text: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Update an existing Teams message by activity ID.

        Used to update the thinking placeholder with tool status
        or replace it with the final answer.
        """
        if not await self._ensure_credentials():
            return False

        access_token = await self._get_access_token()
        if not access_token:
            return False

        url = (
            f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities/{activity_id}"
        )
        activity: dict[str, Any] = {"type": "message"}
        if payload:
            activity.update(payload)
        if text:
            activity["text"] = text

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.put(url, json=activity, headers=headers) as response,
            ):
                if response.status in (200, 201):
                    return True
                else:
                    response_text = await response.text()
                    logger.warning(
                        "Failed to update Teams activity: status=%s, response=%s",
                        response.status,
                        response_text[:200],
                    )
                    return False
        except Exception:
            logger.debug("Failed to update Teams activity", exc_info=True)
            return False

    async def close(self) -> None:
        """Cleanup resources. Called when service is disposed."""
        pass
