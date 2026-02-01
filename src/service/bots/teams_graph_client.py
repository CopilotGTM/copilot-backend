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

import os

import aiohttp
from src.infra.config.configs import secret_manager_config, teams_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


async def get_graph_access_token() -> str | None:
    """Get an access token for Microsoft Graph API using the bot's credentials.

    Uses the bot's app_id/app_secret from Key Vault with the Graph API scope.
    Requires the Azure AD app registration to have `Chat.Read.All` application permission.

    Returns:
        Access token string, or None if authentication fails.
    """
    app_id = await get_master_secret(teams_config.SECRET_NAME_BOT_APP_ID)
    app_secret = await get_master_secret(teams_config.SECRET_NAME_BOT_APP_SECRET)
    tenant_id = await get_master_secret(teams_config.SECRET_NAME_BOT_TENANT_ID)

    if not app_id or not app_secret:
        logger.error(
            "Graph API auth failed: bot credentials not found in Key Vault %s",
            secret_manager_config.AZURE_MASTER_KEY_VAULT_URL,
        )
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


# ---------------------------------------------------------------------------
# Key Vault
# ---------------------------------------------------------------------------


async def get_master_secret(secret_name: str) -> str | None:
    """Fetch a secret from the Master Key Vault.

    Args:
        secret_name: Name of the secret to retrieve.

    Returns:
        Secret value string, or None if not found.
    """
    vault_url = secret_manager_config.AZURE_MASTER_KEY_VAULT_URL
    if not vault_url:
        logger.error("AZURE_MASTER_KEY_VAULT_URL not configured")
        return None

    try:
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

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
                value: str | None = secret.value
                return value
        finally:
            await credential.close()
    except Exception as e:
        logger.warning("Failed to get secret '%s' from Master Key Vault: %s", secret_name, e)
        return None
