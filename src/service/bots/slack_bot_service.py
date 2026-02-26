"""Slack Bot Service.

Handles incoming events from Slack via the Events API.
This service processes Slack events (app_mention, message.im) and sends responses.

Multi-tenant architecture:
- Signing secret is global (your Slack app's secret, from Key Vault)
- Bot token is per-workspace (from bot_platform_installations table)

Identity Flow:
1. User sends message -> Bot checks external_identity_mappings
2. If linked -> Route to agent, update conversation context
3. If not linked -> Generate login URL with secure state token
"""

import json
from typing import Any

import aiohttp
from src.domain.models.master_tables import User
from src.domain.models.master_tables.bot_platform_installation_models import BotPlatformProvider
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.infra.config.configs import slack_config
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import BotAuthStateService, get_bot_auth_state_service
from src.service.bots.auth_validators.base import AuthValidationResult
from src.service.bots.auth_validators.slack_auth import SlackAuthValidator
from src.service.bots.bot_installation_service import BotInstallationService
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)

SLACK_API_BASE = "https://slack.com/api"


class SlackBotService:
    """Handles Slack bot interactions with identity-aware routing.

    Multi-tenant: each workspace has its own bot token stored in
    bot_platform_installations. The signing secret is global (app-level).
    """

    def __init__(self):
        self._signing_secret: str | None = None
        self._app_id: str | None = None
        self._app_credentials_loaded = False
        self._identity_service = ExternalIdentityService()
        self._installation_service = BotInstallationService()
        self._auth_state_service: BotAuthStateService | None = None
        self._auth_validator: SlackAuthValidator | None = None

    @property
    def auth_state_service(self) -> BotAuthStateService:
        """Lazy-load the auth state service."""
        if self._auth_state_service is None:
            self._auth_state_service = get_bot_auth_state_service()
        return self._auth_state_service

    async def _ensure_app_credentials(self) -> bool:
        """Load global app credentials (signing secret) from Key Vault."""
        if self._app_credentials_loaded:
            return bool(self._signing_secret)

        self._signing_secret, self._app_id, _, _ = await slack_config.get_credentials()
        self._app_credentials_loaded = True

        if not self._signing_secret:
            logger.error("Slack signing secret not available")
            return False

        return True

    async def _get_workspace_token(self, team_id: str) -> str | None:
        """Look up the bot token for a workspace from bot_platform_installations."""
        try:
            async for session in acquire_master_async_session():
                token = await self._installation_service.get_bot_token(
                    session, BotPlatformProvider.SLACK, team_id
                )
                if not token:
                    logger.warning("No active Slack installation for team=%s", team_id)
                return token
        except Exception as e:
            logger.error("Failed to look up workspace token for team=%s: %s", team_id, e)
            return None
        return None

    async def validate_request(self, headers: dict[str, str], body: bytes) -> AuthValidationResult:
        """Validate the Slack request signature using global signing secret."""
        if not await self._ensure_app_credentials():
            return AuthValidationResult(False, "credentials_unavailable")

        if not self._signing_secret:
            return AuthValidationResult(False, "signing_secret_missing")

        if self._auth_validator is None:
            self._auth_validator = SlackAuthValidator(self._signing_secret)

        return await self._auth_validator.validate(headers, body)

    async def process_event(self, event_payload: dict[str, Any]) -> None:
        """Process an incoming Slack event.

        The Events API wraps events in an envelope:
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention" | "message",
                ...
            },
            ...
        }
        """
        event = event_payload.get("event", {})
        event_type = event.get("type", "")

        # Ignore bot messages and non-user subtypes to prevent loops.
        # chat.update triggers message_changed events; bot posts have bot_id.
        subtype = event.get("subtype", "")
        if event.get("bot_id") or subtype in (
            "bot_message",
            "message_changed",
            "message_deleted",
            "message_replied",
            "channel_join",
            "channel_leave",
        ):
            logger.debug("Ignoring message subtype=%s bot_id=%s", subtype, event.get("bot_id"))
            return

        if event_type == "app_mention":
            await self._handle_mention(event, event_payload)
        elif event_type == "message":
            # message events in DMs (im channel type)
            channel_type = event.get("channel_type", "")
            if channel_type == "im":
                await self._handle_dm(event, event_payload)
            else:
                logger.debug("Ignoring non-DM message event: channel_type=%s", channel_type)
        else:
            logger.debug("Unhandled Slack event type: %s", event_type)

    async def process_interaction(self, payload: dict[str, Any]) -> None:
        """Process a Slack interactive component submission.

        Handles block_actions (button clicks) and view_submission (modal forms).
        Normalizes the Slack payload into the activity/form_data format expected
        by the shared FormHandlerRegistry.
        """
        interaction_type = payload.get("type", "")

        if interaction_type == "block_actions":
            await self._handle_block_action(payload)
        elif interaction_type == "view_submission":
            await self._handle_view_submission(payload)
        else:
            logger.debug("Unhandled Slack interaction type: %s", interaction_type)

    async def _handle_block_action(self, payload: dict[str, Any]) -> None:
        """Handle a block_actions payload (button click, select change, etc.)."""
        from src.service.bots.form_handlers import FormHandlerRegistry

        actions = payload.get("actions", [])
        if not actions:
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        team_id = payload.get("team", {}).get("id", "")
        user_id = payload.get("user", {}).get("id", "")
        channel_id = payload.get("channel", {}).get("id", "")

        # Build activity-like dict for form handlers (platform-agnostic)
        activity = {
            "from": {"id": user_id},
            "channelData": {"team": {"id": team_id}},
            "provider": "slack",
        }

        # Extract form data from the action value
        # For buttons, the value is a string; for other elements, it varies
        action_value = action.get("value", "")
        if isinstance(action_value, str):
            try:
                form_data = json.loads(action_value)
            except (json.JSONDecodeError, TypeError):
                form_data = {"value": action_value}
        else:
            form_data = action_value if isinstance(action_value, dict) else {}

        form_data["action"] = action_id

        # Also extract values from the message's block state (for input fields)
        block_state = payload.get("state", {}).get("values", {})
        for _block_id, block_values in block_state.items():
            for field_id, field_data in block_values.items():
                field_type = field_data.get("type", "")
                if field_type == "plain_text_input":
                    form_data[field_id] = field_data.get("value", "")
                elif field_type == "static_select":
                    selected = field_data.get("selected_option")
                    form_data[field_id] = selected.get("value", "") if selected else ""
                elif field_type == "datepicker":
                    form_data[field_id] = field_data.get("selected_date", "")
                elif field_type == "checkboxes":
                    selected = field_data.get("selected_options", [])
                    form_data[field_id] = [o.get("value", "") for o in selected]

        result = await FormHandlerRegistry.handle(action_id, activity, form_data)

        if not result:
            logger.debug("No handler found for Slack action: %s", action_id)
            return

        # Send response back to the channel
        if team_id and channel_id:
            from src.adapters.slack_adapter import markdown_to_mrkdwn

            response_text = markdown_to_mrkdwn(result.message) if result.message else ""

            if result.card_payload:
                # card_payload may contain blocks for Slack
                blocks = result.card_payload.get("blocks")
                if blocks:
                    await self.send_block_kit_message(team_id, channel_id, blocks, response_text)
                    return

            if response_text:
                await self._send_message(team_id, channel_id, response_text)

    async def _handle_view_submission(self, payload: dict[str, Any]) -> None:
        """Handle a view_submission payload (modal form submit)."""
        from src.service.bots.form_handlers import FormHandlerRegistry

        view = payload.get("view", {})
        callback_id = view.get("callback_id", "")
        team_id = payload.get("team", {}).get("id", "")
        user_id = payload.get("user", {}).get("id", "")

        if not callback_id:
            return

        activity = {
            "from": {"id": user_id},
            "channelData": {"team": {"id": team_id}},
            "provider": "slack",
        }

        # Flatten view state values into form_data
        form_data: dict[str, Any] = {"action": callback_id}
        state_values = view.get("state", {}).get("values", {})
        for block_values in state_values.values():
            for field_id, field_data in block_values.items():
                field_type = field_data.get("type", "")
                if field_type == "plain_text_input":
                    form_data[field_id] = field_data.get("value", "")
                elif field_type == "static_select":
                    selected = field_data.get("selected_option")
                    form_data[field_id] = selected.get("value", "") if selected else ""
                elif field_type == "datepicker":
                    form_data[field_id] = field_data.get("selected_date", "")
                elif field_type == "checkboxes":
                    selected = field_data.get("selected_options", [])
                    form_data[field_id] = [o.get("value", "") for o in selected]

        result = await FormHandlerRegistry.handle(callback_id, activity, form_data)

        if not result:
            logger.debug("No handler for Slack view submission: %s", callback_id)
            return

        # For view submissions, we can't easily send to a channel
        # (no channel_id in view_submission). Use DM to the user.
        if team_id and user_id:
            from src.adapters.slack_adapter import markdown_to_mrkdwn

            response_text = markdown_to_mrkdwn(result.message) if result.message else ""
            if response_text:
                # Open a DM with the user and send the response
                dm_channel = await self._open_dm(team_id, user_id)
                if dm_channel:
                    await self._send_message(team_id, dm_channel, response_text)

    async def _open_dm(self, team_id: str, user_id: str) -> str | None:
        """Open a DM channel with a user via conversations.open."""
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            return None
        try:
            url = f"{SLACK_API_BASE}/conversations.open"
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(
                    url,
                    headers={"Authorization": f"Bearer {bot_token}"},
                    json={"users": user_id},
                ) as resp,
            ):
                data = await resp.json()
                if data.get("ok"):
                    return data.get("channel", {}).get("id")
                logger.warning("Failed to open DM: %s", data.get("error"))
        except Exception:
            logger.debug("Failed to open DM with user %s", user_id, exc_info=True)
        return None

    async def _handle_mention(self, event: dict[str, Any], envelope: dict[str, Any]) -> None:
        """Handle @mention in a channel."""
        slack_user_id = event.get("user", "")
        slack_team_id = envelope.get("team_id", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")

        # Strip the bot mention from the text (e.g., "<@U12345> what is...")
        text = self._strip_bot_mention(text)

        if not text.strip():
            await self._send_message(
                slack_team_id, channel_id, "I received an empty message. How can I help?"
            )
            return

        await self._route_message(
            text=text,
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            conversation_type="channel",
        )

    async def _handle_dm(self, event: dict[str, Any], envelope: dict[str, Any]) -> None:
        """Handle direct message to the bot."""
        slack_user_id = event.get("user", "")
        slack_team_id = envelope.get("team_id", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")

        if not text.strip():
            await self._send_message(
                slack_team_id, channel_id, "I received an empty message. How can I help?"
            )
            return

        await self._route_message(
            text=text,
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            conversation_type="personal",
        )

    async def _route_message(
        self,
        text: str,
        slack_user_id: str,
        slack_team_id: str,
        channel_id: str,
        conversation_type: str,
    ) -> None:
        """Route message based on identity mapping."""
        user, _ = await self._get_linked_user(slack_user_id, slack_team_id)

        if not user:
            login_message = await self._get_login_message_with_state(
                slack_user_id=slack_user_id,
                slack_team_id=slack_team_id,
                channel_id=channel_id,
            )
            await self._send_message(slack_team_id, channel_id, login_message)
            return

        # Fetch user timezone and send thinking indicator in parallel
        user_info = await self._fetch_slack_user_info(slack_team_id, slack_user_id)
        thinking_ts = await self._send_thinking_indicator(slack_team_id, channel_id)

        response = await self._handle_authenticated_message(
            text=text,
            user=user,
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            conversation_type=conversation_type,
            external_user_id=slack_user_id,
            external_tenant_id=slack_team_id,
            thinking_message_ts=thinking_ts,
            user_timezone=user_info.get("tz"),
        )

        if response:
            # Error message from failed enqueue — update thinking or send new
            if thinking_ts:
                await self._update_message(slack_team_id, channel_id, thinking_ts, response)
            else:
                await self._send_message(slack_team_id, channel_id, response)

    async def _get_linked_user(
        self, slack_user_id: str, slack_team_id: str
    ) -> tuple[User | None, dict[str, Any] | None]:
        """Look up linked CopilotGTM user for Slack identity."""
        try:
            async for session in acquire_master_async_session():
                user, mapping = await self._identity_service.get_user_for_external_identity(
                    session,
                    ExternalIdentityProvider.SLACK,
                    slack_user_id,
                    slack_team_id,
                )
                if user and mapping:
                    return user, mapping.conversation_context
                return None, None
            return None, None
        except Exception as e:
            logger.error("Failed to look up Slack identity: %s", e, exc_info=True)
            return None, None

    async def _handle_authenticated_message(
        self,
        text: str,
        user: User,
        slack_team_id: str,
        channel_id: str,
        conversation_type: str = "personal",
        external_user_id: str = "",
        external_tenant_id: str = "",
        thinking_message_ts: str | None = None,
        user_timezone: str | None = None,
    ) -> str | None:
        """Handle a message from an authenticated user.

        Enqueues a Celery task to process the message through the GeneralAgent.
        The agent response will be sent back as a proactive message.

        Returns an error string only if enqueue fails.
        """
        from src.workers.chat_worker.client import send_chat_task_async

        bot_context: dict[str, str] = {
            "conversation_type": conversation_type,
            "slack_channel_id": channel_id,
            "external_user_id": external_user_id,
            "external_tenant_id": external_tenant_id,
        }
        if user_timezone:
            bot_context["user_timezone"] = user_timezone

        reply_context: dict[str, str] = {
            "channel_id": channel_id,
            "team_id": slack_team_id,
        }
        if thinking_message_ts:
            reply_context["thinking_message_ts"] = thinking_message_ts

        try:
            task_id = await send_chat_task_async(
                "chat.process_bot_message",
                args=[
                    str(user.organisation_id),
                    str(user.user_id),
                    text,
                    "slack",
                    reply_context,
                ],
                kwargs={
                    "user_email": user.email,
                    "bot_context": bot_context,
                },
                queue="chat_messages",
            )
            logger.info(
                "Enqueued Slack bot message task=%s for user=%s org=%s type=%s",
                task_id,
                user.email,
                user.organisation_id,
                conversation_type,
            )
        except Exception as e:
            logger.error("Failed to enqueue Slack bot message task: %s", e, exc_info=True)
            return (
                "I'm sorry, I'm having trouble processing your request right now. "
                "Please try again in a moment."
            )

        return None

    async def _fetch_slack_user_info(self, team_id: str, user_id: str) -> dict[str, str | None]:
        """Fetch display name and timezone for a Slack user via users.info API.

        Returns dict with 'name' and 'tz' keys (either may be None).
        """
        result: dict[str, str | None] = {"name": None, "tz": None}
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            return result
        try:
            url = f"{SLACK_API_BASE}/users.info"
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.get(
                    url,
                    headers={"Authorization": f"Bearer {bot_token}"},
                    params={"user": user_id},
                ) as resp,
            ):
                data = await resp.json()
                if data.get("ok"):
                    user_data = data.get("user", {})
                    profile = user_data.get("profile", {})
                    result["name"] = (
                        profile.get("display_name")
                        or profile.get("real_name")
                        or user_data.get("real_name")
                    )
                    result["tz"] = user_data.get("tz")
        except Exception:
            logger.debug("Failed to fetch Slack user info for %s", user_id, exc_info=True)
        return result

    async def _get_login_message_with_state(
        self,
        slack_user_id: str,
        slack_team_id: str,
        channel_id: str,
    ) -> str:
        """Generate login prompt with secure state token."""
        if not slack_config.SLACK_AUTH_LOGIN_URL:
            return (
                "*Sign In Required*\n\n"
                "Please sign in to your CopilotGTM account to use this bot.\n"
                "Contact your administrator to get the login URL."
            )

        user_info = await self._fetch_slack_user_info(slack_team_id, slack_user_id)

        state = self.auth_state_service.create_state(
            provider=ExternalIdentityProvider.SLACK,
            external_user_id=slack_user_id,
            external_tenant_id=slack_team_id,
            external_user_name=user_info.get("name"),
            context={
                "channel_id": channel_id,
            },
        )

        login_url = self.auth_state_service.generate_login_url(state)

        if not login_url:
            return (
                "*Sign In Required*\n\n"
                "Unable to generate login link. Please contact your administrator."
            )

        return (
            "*Sign In Required*\n\n"
            f"Please sign in to your CopilotGTM account:\n\n"
            f"<{login_url}|Click here to sign in>\n\n"
            "After signing in, return here and try your question again.\n\n"
            "_This link expires in 5 minutes._"
        )

    async def _send_thinking_indicator(self, team_id: str, channel_id: str) -> str | None:
        """Send a 'Thinking...' placeholder message and return its ts for later update."""
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            return None

        url = f"{SLACK_API_BASE}/chat.postMessage"
        payload = {
            "channel": channel_id,
            "text": ":hourglass_flowing_sand: _Thinking..._",
        }
        try:
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                data = await resp.json()
                if data.get("ok"):
                    return data.get("ts")
        except Exception:
            logger.warning("Failed to send thinking indicator", exc_info=True)
        return None

    async def _update_message(
        self, team_id: str, channel_id: str, message_ts: str, text: str
    ) -> bool:
        """Update an existing Slack message (e.g. replace thinking indicator)."""
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            logger.warning("Cannot update Slack message: no token for team=%s", team_id)
            return False

        url = f"{SLACK_API_BASE}/chat.update"
        payload = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
        }
        try:
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(
                        "Slack chat.update failed: %s (team=%s, ts=%s)",
                        data.get("error", "unknown"),
                        team_id,
                        message_ts,
                    )
                return data.get("ok", False)
        except Exception:
            logger.warning("Failed to update Slack message", exc_info=True)
            return False

    async def _update_message_blocks(
        self,
        team_id: str,
        channel_id: str,
        message_ts: str,
        blocks: list[dict[str, Any]],
        text_fallback: str,
    ) -> bool:
        """Update an existing Slack message with Block Kit blocks."""
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            logger.warning("Cannot update Slack message blocks: no token for team=%s", team_id)
            return False

        url = f"{SLACK_API_BASE}/chat.update"
        payload: dict[str, Any] = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text_fallback,
            "blocks": blocks,
        }
        try:
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(
                        "Slack chat.update (blocks) failed: %s (team=%s, ts=%s, block_count=%d)",
                        data.get("error", "unknown"),
                        team_id,
                        message_ts,
                        len(blocks),
                    )
                return data.get("ok", False)
        except Exception:
            logger.warning("Failed to update Slack message with blocks", exc_info=True)
            return False

    async def _send_message(self, team_id: str, channel_id: str, text: str) -> bool:
        """Send a message to a Slack channel via Web API.

        Looks up the per-workspace bot token from bot_platform_installations.
        """
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            logger.error("Cannot send Slack message: no token for team=%s", team_id)
            return False

        url = f"{SLACK_API_BASE}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "channel": channel_id,
            "text": text,
        }

        try:
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(url, json=payload, headers=headers) as response,
            ):
                result = await response.json()
                if result.get("ok"):
                    logger.debug("Slack message sent to channel=%s team=%s", channel_id, team_id)
                    return True
                else:
                    logger.error(
                        "Slack API error: %s (team=%s)", result.get("error", "unknown"), team_id
                    )
                    return False
        except Exception as e:
            logger.error("Failed to send Slack message: %s", e, exc_info=True)
            return False

    async def send_block_kit_message(
        self, team_id: str, channel_id: str, blocks: list[dict[str, Any]], text_fallback: str
    ) -> bool:
        """Send a Block Kit message to a Slack channel.

        Looks up the per-workspace bot token from bot_platform_installations.
        """
        bot_token = await self._get_workspace_token(team_id)
        if not bot_token:
            logger.error("Cannot send Block Kit message: no token for team=%s", team_id)
            return False

        url = f"{SLACK_API_BASE}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "channel": channel_id,
            "blocks": blocks,
            "text": text_fallback,
        }

        try:
            async with (
                aiohttp.ClientSession() as http_session,
                http_session.post(url, json=payload, headers=headers) as response,
            ):
                result = await response.json()
                if result.get("ok"):
                    logger.info(
                        "Slack Block Kit message sent to channel=%s team=%s", channel_id, team_id
                    )
                    return True
                else:
                    logger.error(
                        "Slack Block Kit API error: %s (team=%s)",
                        result.get("error", "unknown"),
                        team_id,
                    )
                    return False
        except Exception as e:
            logger.error("Failed to send Slack Block Kit message: %s", e, exc_info=True)
            return False

    @staticmethod
    def _strip_bot_mention(text: str) -> str:
        """Strip bot @mention from message text.

        Slack formats mentions as <@U12345ABC>, so we remove this pattern.
        """
        import re

        return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    async def close(self) -> None:
        """Cleanup resources."""
        pass
