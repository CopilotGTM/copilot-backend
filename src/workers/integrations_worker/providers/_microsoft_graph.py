"""Shared helpers for direct Microsoft Graph API calls.

OAuth tokens are managed by Nango (token storage + refresh) but API calls
bypass the Nango proxy to avoid proxy request quota limits.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com"


async def get_access_token(
    nango: NangoClient,
    connection_id: str,
    provider_config_key: str,
) -> str:
    """Retrieve a fresh OAuth access token from Nango.

    Uses Nango's connection API (not proxy) to get/refresh the token.
    """
    connection = await nango.get_connection(
        provider=provider_config_key,
        connection_id=connection_id,
        force_refresh=True,
    )
    credentials = connection.get("credentials", {})
    token = credentials.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in Nango connection for {connection_id}")
    return str(token)


async def graph_api_request(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a GET request to the Microsoft Graph API.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = f"{GRAPH_BASE_URL}{endpoint}"
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


async def graph_api_request_url(
    client: httpx.AsyncClient,
    url: str,
) -> dict[str, Any]:
    """Follow an opaque Microsoft Graph URL (@odata.nextLink or @odata.deltaLink).

    These URLs must be used as-is — Microsoft Graph encodes pagination state
    into them and any URL decode/re-encode corrupts the tokens.
    """
    response = await client.get(url)
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]
