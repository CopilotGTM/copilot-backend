"""Shared helpers for direct Microsoft Graph API calls.

OAuth tokens are managed by custom OAuth (token storage + refresh via
token_resolution module). API calls go directly to Microsoft Graph.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
    get_access_token,
)

logger = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com"

__all__ = [
    "IntegrationTokenInfo",
    "call_with_token_retry",
    "get_access_token",
    "graph_api_request",
    "graph_api_request_url",
]


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
