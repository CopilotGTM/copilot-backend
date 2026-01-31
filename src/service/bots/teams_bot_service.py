"""Microsoft Teams Bot Service.

Handles incoming messages from Teams via Azure Bot Service.
This service processes Bot Framework activities and sends responses.

Bot credentials are fetched from Azure Master Key Vault at runtime.

Identity Flow:
1. User sends message -> Bot checks external_identity_mappings
2. If linked -> Route to agent (TODO), update conversation context
3. If not linked -> Generate login URL with secure state token
"""

from typing import Any

import aiohttp
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.infra.config.configs import teams_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.secrets.manager import master_secrets_manager
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
        """Load bot credentials from Master Key Vault if not already loaded."""
        if self._credentials_loaded:
            return bool(self._app_id and self._app_secret)

        logger.info("Loading Teams bot credentials from Master Key Vault")
        self._app_id = await master_secrets_manager.get(teams_config.SECRET_NAME_BOT_APP_ID)
        self._app_secret = await master_secrets_manager.get(teams_config.SECRET_NAME_BOT_APP_SECRET)
        self._tenant_id = await master_secrets_manager.get(teams_config.SECRET_NAME_BOT_TENANT_ID)

        self._credentials_loaded = True

        if not self._app_id or not self._app_secret:
            logger.error("Teams bot credentials not found in Master Key Vault")
            return False

        logger.info("Teams bot credentials loaded successfully")
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

    async def process_activity(self, activity: dict[str, Any]) -> None:
        """
        Process incoming activity from Teams.

        Args:
            activity: The Bot Framework activity (message, event, etc.)
        """
        activity_type = activity.get("type", "")

        if activity_type == "message":
            await self._handle_message(activity)
        elif activity_type == "conversationUpdate":
            await self._handle_conversation_update(activity)
        elif activity_type == "invoke":
            self._handle_invoke(activity)
        else:
            logger.debug("Unhandled activity type: %s", activity_type)

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
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        # Verifying User Identity
        user = await self._get_linked_user(teams_user_id, teams_tenant_id)
        if not user:
            login_message = self._get_login_message_with_state(
                teams_user_id, teams_tenant_id, teams_user_name, service_url, conversation_id
            )
            await self._send_reply(activity, login_message, service_url)
            return

        text: str = activity.get("text") or ""
        if not text.strip():
            logger.info("Ignoring empty message from user %s", user.email)
            await self._send_reply(activity, "I received an empty message.", service_url)
            return

        if conversation_type == "personal":
            text = activity.get("text") or ""
            response = await self._handle_authenticated_message(
                text, user, service_url, conversation_id, conversation_type
            )

        elif conversation_type in ["groupChat", "channel", "meeting"]:
            response = await self._handle_authenticated_message(
                text, user, service_url, conversation_id, conversation_type
            )
        else:
            logger.warning("Teams unexpected conversationType: %s", conversation_type)
            await self._send_reply(
                activity,
                "Invalid message format. Unable to respond.",
                service_url,
            )
            return

        # Send response back to Teams
        await self._send_reply(activity, response, service_url)

    async def _get_linked_user(self, teams_user_id: str, teams_tenant_id: str) -> User | None:
        """Look up linked CopilotGTM user for Teams identity."""
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.TEAMS,
                    teams_user_id,
                    teams_tenant_id,
                )
                if user and mapping:
                    return user
                return None
            return None
        except Exception as e:
            logger.error("Failed to look up Teams identity: %s", e, exc_info=True)
            return None

    async def _handle_authenticated_message(
        self,
        text: str,
        user: User,
        service_url: str,
        conversation_id: str,
        conversation_type: str = "personal",
    ) -> str:
        """Handle a message from an authenticated user.

        Enqueues a Celery task to process the message through the GeneralAgent.
        The agent response will be sent back as a proactive message.

        Returns an immediate acknowledgement to the user.
        """
        from src.workers.chat_worker.client import send_chat_task_async

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
                queue="chat_messages",
            )
            logger.info(
                "Enqueued bot message task=%s for user=%s org=%s type=%s",
                task_id,
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

        return "Let me look into that for you..."

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

    def _handle_invoke(self, activity: dict[str, Any]) -> None:
        """Handle invoke activities (adaptive card actions, etc.)."""
        invoke_name = activity.get("name", "")
        logger.debug("Invoke activity received: %s", invoke_name)

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

    async def close(self) -> None:
        """Cleanup resources. Called when service is disposed."""
        pass
