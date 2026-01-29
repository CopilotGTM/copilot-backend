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
    ExternalIdentityMapping,
    ExternalIdentityProvider,
)
from src.infra.config.configs import secret_manager_config, teams_config
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
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

        # Fetch all three secrets from Master Key Vault
        self._app_id = await self._get_master_secret(teams_config.SECRET_NAME_BOT_APP_ID)
        self._app_secret = await self._get_master_secret(teams_config.SECRET_NAME_BOT_APP_SECRET)
        self._tenant_id = await self._get_master_secret(teams_config.SECRET_NAME_BOT_TENANT_ID)

        self._credentials_loaded = True

        if not self._app_id or not self._app_secret:
            logger.error(
                "Teams bot credentials not found in Master Key Vault. "
                "Expected secrets: %s, %s, %s in vault: %s",
                teams_config.SECRET_NAME_BOT_APP_ID,
                teams_config.SECRET_NAME_BOT_APP_SECRET,
                teams_config.SECRET_NAME_BOT_TENANT_ID,
                secret_manager_config.AZURE_MASTER_KEY_VAULT_URL,
            )
            return False

        logger.info("Teams bot credentials loaded successfully")
        return True

    async def _get_master_secret(self, secret_name: str) -> str | None:
        """Fetch a secret from the Master Key Vault."""
        import os

        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        vault_url = secret_manager_config.AZURE_MASTER_KEY_VAULT_URL
        if not vault_url:
            logger.error("AZURE_MASTER_KEY_VAULT_URL not configured")
            return None

        try:
            # Use async DefaultAzureCredential
            if os.getenv("ENV") == "local":
                credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                    exclude_visual_studio_code_credential=False,
                )
            else:
                credential = DefaultAzureCredential()

            try:
                async with SecretClient(vault_url=vault_url, credential=credential) as client:
                    secret = await client.get_secret(secret_name)
                    return secret.value
            finally:
                await credential.close()
        except Exception as e:
            logger.warning("Failed to get secret '%s' from Master Key Vault: %s", secret_name, e)
            return None

    async def process_activity(self, activity: dict[str, Any], _auth_header: str) -> None:
        """
        Process incoming activity from Teams.

        Args:
            activity: The Bot Framework activity (message, event, etc.)
            _auth_header: Authorization header for Bot Framework validation
        """
        # Ensure credentials are loaded
        if not await self._ensure_credentials():
            logger.error("Cannot process activity: bot credentials not available")
            return

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
        text = (activity.get("text") or "").strip()
        from_user = activity.get("from", {})
        service_url = activity.get("serviceUrl", "")
        conversation_id = activity.get("conversation", {}).get("id", "")

        teams_user_id = from_user.get("id", "")
        teams_user_name = from_user.get("name", "")

        # Extract tenant ID from channelData
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        logger.info(
            "Teams message from %s (user_id=%s): %s",
            teams_user_name,
            teams_user_id[:20] if teams_user_id else "unknown",
            text[:50] if text else "(empty)",
        )

        # Check identity mapping
        user, mapping = await self._get_linked_user(teams_user_id, teams_tenant_id)

        # Route command or message
        response = await self._route_message(
            text,
            user,
            mapping,
            teams_user_id,
            teams_tenant_id,
            teams_user_name,
            service_url,
            conversation_id,
        )

        # Send response back to Teams
        await self._send_reply(activity, response, service_url)

    async def _route_message(
        self,
        text: str,
        user: User | None,
        mapping: ExternalIdentityMapping | None,
        teams_user_id: str,
        teams_tenant_id: str,
        teams_user_name: str,
        service_url: str,
        conversation_id: str,
    ) -> str:
        """Route a message to the appropriate handler based on command or auth state."""
        text_lower = text.lower()

        if text_lower == "help":
            return self._get_help_message()

        if text_lower == "status":
            return self._get_status_message(user)

        if text_lower == "logout":
            if user and mapping:
                return await self._handle_logout(teams_user_id, teams_tenant_id, user)
            return "You are not currently signed in."

        if text_lower == "login":
            if user and mapping:
                return f"You are already signed in as **{user.email}**.\n\nType **logout** to sign out."
            return self._get_login_message_with_state(
                teams_user_id, teams_tenant_id, teams_user_name, service_url, conversation_id
            )

        # Regular message
        if user and mapping:
            await self._update_conversation_context(mapping, service_url, conversation_id)
            return await self._handle_authenticated_message(
                text, user, service_url, conversation_id
            )

        return self._get_login_message_with_state(
            teams_user_id, teams_tenant_id, teams_user_name, service_url, conversation_id
        )

    async def _get_linked_user(
        self, teams_user_id: str, teams_tenant_id: str
    ) -> tuple[User | None, ExternalIdentityMapping | None]:
        """Look up linked CopilotGTM user for Teams identity."""
        try:
            async with master_async_session_scope() as session:
                return await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.TEAMS,
                    teams_user_id,
                    teams_tenant_id,
                )
        except Exception as e:
            logger.error("Failed to look up Teams identity: %s", e, exc_info=True)
            return None, None

    async def _update_conversation_context(
        self,
        mapping: ExternalIdentityMapping,
        service_url: str,
        conversation_id: str,
    ) -> None:
        """Update conversation context for proactive messaging."""
        try:
            async with master_async_session_scope() as session:
                await self._identity_service.update_conversation_context(
                    session,
                    mapping.mapping_id,
                    {"service_url": service_url, "conversation_id": conversation_id},
                )
        except Exception as e:
            logger.warning("Failed to update conversation context: %s", e)

    async def _handle_logout(self, teams_user_id: str, teams_tenant_id: str, user: User) -> str:
        """Handle logout command - deactivate identity mapping."""
        try:
            async with master_async_session_scope() as session:
                deactivated = await self._identity_service.deactivate_mapping(
                    session,
                    ExternalIdentityProvider.TEAMS,
                    teams_user_id,
                    teams_tenant_id,
                )

            if deactivated:
                logger.info("User %s logged out from Teams", user.email)
                return (
                    f"You have been signed out ({user.email}).\n\nType **login** to sign in again."
                )
            else:
                return "Failed to sign out. Please try again."

        except Exception as e:
            logger.error("Failed to handle logout: %s", e, exc_info=True)
            return "An error occurred while signing out. Please try again."

    async def _handle_authenticated_message(
        self,
        text: str,
        user: User,
        service_url: str,
        conversation_id: str,
    ) -> str:
        """Handle a message from an authenticated user.

        Enqueues a Celery task to process the message through the GeneralAgent.
        The agent response will be sent back as a proactive message.

        Returns an immediate acknowledgement to the user.
        """
        from src.workers.chat_worker.client import send_chat_task_async

        logger.info(
            "Authenticated message from %s: %s",
            user.email,
            text[:50] if text else "(empty)",
        )

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
                "Enqueued bot message task=%s for user=%s org=%s",
                task_id,
                user.email,
                user.organisation_id,
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

    def _get_help_message(self) -> str:
        """Generate help message."""
        return (
            "**CopilotGTM Bot Commands:**\n\n"
            "- **login** - Sign in to your CopilotGTM account\n"
            "- **logout** - Sign out of your account\n"
            "- **status** - Check your authentication status\n"
            "- **help** - Show this help message\n\n"
            "Once signed in, just type your question and I'll help you find answers "
            "from your organization's knowledge base!"
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

    def _get_status_message(self, user: User | None) -> str:
        """Generate status message based on identity mapping."""
        if user:
            org_name = "Unknown"
            if hasattr(user, "organisation") and user.organisation:
                org_name = user.organisation.organisation_name
            return (
                f"**Signed In**\n\n"
                f"You are signed in as **{user.email}**\n"
                f"Organisation: {org_name}\n\n"
                "Type **logout** to sign out."
            )
        else:
            return (
                "**Not Signed In**\n\n"
                "You are not currently signed in.\n"
                "Type **login** to sign in to your CopilotGTM account."
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
                    return result.get("access_token")
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

    def close(self) -> None:
        """Cleanup resources. Called when service is disposed."""
        # No persistent resources to clean up currently
