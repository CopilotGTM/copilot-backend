"""Shared Microsoft Graph API client for Teams bot operations.

Provides authentication and utility functions for calling the Microsoft Graph API
using the bot's Azure AD credentials (client credentials flow).

Security notes:
- Uses application-level permissions:
  - Chat.Read.All — for reading chat messages (group chat tool).

Used by:
- fetch_teams_chat_messages tool (group chat history)
"""

from __future__ import annotations

import aiohttp
from src.infra.config.configs import teams_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


async def get_graph_access_token() -> str | None:
    """Get an access token for Microsoft Graph API using the bot's credentials.

    Uses the bot's app_id/app_secret from Key Vault with the Graph API scope.
    Requires the Azure AD app registration to have `Chat.Read.All` application permission.

    Returns:
        Access token string, or None if authentication fails.
    """
    app_id, app_secret, tenant_id = await teams_config.get_bot_credentials()

    if not app_id or not app_secret:
        logger.error("Graph API auth failed: bot credentials not available")
        return None

    tenant = tenant_id or "common"
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    data = {
        "grant_type": "client_credentials",
        "client_id": app_id,
        "client_secret": app_secret,
        "scope": "https://graph.microsoft.com/.default",
    }

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(token_url, data=data) as response,
        ):
            if response.status == 200:
                result = await response.json()
                token: str | None = result.get("access_token")
                return token
            else:
                response_text = await response.text()
                logger.error(
                    "Graph API token request failed: status=%s response=%s",
                    response.status,
                    response_text[:200],
                )
                return None
    except Exception as e:
        logger.exception("Graph API token request error: %s", e)
        return None
