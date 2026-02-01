"""Fetch Teams group chat messages tool for agents.

Tool for fetching recent messages from a Microsoft Teams group chat
where the bot was @mentioned. Uses Microsoft Graph API with the bot's
Azure AD credentials (client credentials flow).

Requires `Chat.Read.All` application permission on the Azure AD app registration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def fetch_teams_chat_messages(
    *,
    context: AgentContext,
    page_size: int = 50,
    skip_token: str | None = None,
) -> dict[str, Any]:
    """Fetch recent messages from a Teams group chat via Microsoft Graph API.

    Uses the bot's Azure AD credentials (client credentials flow) to call
    GET /v1.0/chats/{chat-id}/messages.

    Args:
        context: AgentContext containing metadata with teams_conversation_id.
        page_size: Number of messages to fetch (default: 50, max: 50).
        skip_token: Pagination token from a previous response.

    Returns:
        Dict with messages list, has_more flag, and optional skip_token.
    """
    conversation_id = context.metadata.get("teams_conversation_id", "")
    if not conversation_id:
        logger.warning("fetch_teams_chat_messages: no teams_conversation_id in context metadata")
        return {
            "messages": [],
            "has_more": False,
            "skip_token": None,
            "error": "This tool is only available in group chat contexts.",
        }

    # Cap page_size to Graph API max
    page_size = min(max(page_size, 1), 50)

    # Get access token for Microsoft Graph
    from src.service.bots.teams_graph_client import get_graph_access_token

    access_token = await get_graph_access_token()
    if not access_token:
        logger.error("fetch_teams_chat_messages: failed to get Graph API access token")
        return {
            "messages": [],
            "has_more": False,
            "skip_token": None,
            "error": "Failed to authenticate with Microsoft Graph API.",
        }

    # Build Graph API URL
    import urllib.parse

    base_url = f"https://graph.microsoft.com/v1.0/chats/{conversation_id}/messages"
    params: dict[str, str] = {"$top": str(page_size)}
    if skip_token:
        params["$skiptoken"] = skip_token

    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    # Call Graph API
    import aiohttp

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers) as response,
        ):
            if response.status != 200:
                response_text = await response.text()
                logger.error(
                    "fetch_teams_chat_messages: Graph API error status=%s response=%s",
                    response.status,
                    response_text[:300],
                )
                return {
                    "messages": [],
                    "has_more": False,
                    "skip_token": None,
                    "error": f"Graph API returned status {response.status}.",
                }

            data = await response.json()

    except Exception as e:
        logger.exception("fetch_teams_chat_messages: Graph API request failed: %s", e)
        return {
            "messages": [],
            "has_more": False,
            "skip_token": None,
            "error": "Failed to fetch messages from Microsoft Graph API.",
        }

    # Parse messages
    messages = []
    for msg in data.get("value", []):
        body = msg.get("body", {})
        content = body.get("content", "")
        content_type = body.get("contentType", "text")

        # For HTML content, do a basic strip of tags
        if content_type == "html" and content:
            import re

            content = re.sub(r"<[^>]+>", "", content).strip()

        sender_info = msg.get("from", {})
        sender_user = sender_info.get("user", {}) if sender_info else {}
        sender_name = sender_user.get("displayName", "Unknown") if sender_user else "System"

        messages.append(
            {
                "sender": sender_name,
                "text": content,
                "timestamp": msg.get("createdDateTime", ""),
                "message_type": msg.get("messageType", ""),
            }
        )

    # Check for pagination
    next_link = data.get("@odata.nextLink", "")
    next_skip_token: str | None = None
    has_more = bool(next_link)

    if next_link:
        # Extract skiptoken from the next link
        import urllib.parse as urlparse

        parsed = urlparse.urlparse(next_link)
        query_params = urlparse.parse_qs(parsed.query)
        skip_tokens = query_params.get("$skiptoken", [])
        next_skip_token = skip_tokens[0] if skip_tokens else None

    return {
        "messages": messages,
        "has_more": has_more,
        "skip_token": next_skip_token,
    }


def create_fetch_teams_chat_messages(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create fetch_teams_chat_messages tool bound to a context."""

    async def fetch_teams_chat_messages_tool(
        page_size: int = 50,
        skip_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch recent messages from the Teams group chat where you were @mentioned.

        Use this tool to understand the conversation context in a group chat.
        Start with the default page_size of 50. If you need more context,
        call again with the skip_token from the previous response.

        Args:
            page_size: Number of messages to fetch (default: 50, max: 50).
            skip_token: Pagination token from a previous response to get more messages.

        Returns:
            A dict containing:
            - messages: List of messages with sender, text, and timestamp
            - has_more: Whether more messages are available
            - skip_token: Token to fetch the next page (pass back to get more)
        """
        logger.info(
            "[TOOL CALLED] fetch_teams_chat_messages - page_size=%s, has_skip_token=%s",
            page_size,
            bool(skip_token),
        )

        # Check if we're in a group chat context
        if context.metadata.get("conversation_type") != "groupChat":
            logger.info("[TOOL RESULT] fetch_teams_chat_messages - not in group chat context")
            return {
                "messages": [],
                "has_more": False,
                "skip_token": None,
                "error": "This tool is only available when you are @mentioned in a group chat.",
            }

        from src.agents.tools.base import execute_with_capabilities

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="FetchTeamsChatMessages",
            tool_func=fetch_teams_chat_messages,
            parameters={
                "context": context,
                "page_size": page_size,
                "skip_token": skip_token,
            },
            description="Fetching Teams group chat messages",
            fallback_value={
                "messages": [],
                "has_more": False,
                "skip_token": None,
                "error": "Failed to fetch chat messages.",
            },
        )

        msg_count = len(result.get("messages", [])) if isinstance(result, dict) else 0
        logger.info("[TOOL RESULT] fetch_teams_chat_messages returned %s messages", msg_count)
        return result

    return fetch_teams_chat_messages_tool
