"""Shared helpers for direct HubSpot API calls.

OAuth tokens are managed by Nango (token storage + refresh) but API calls
bypass the Nango proxy to avoid proxy request quota limits.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)

HUBSPOT_BASE_URL = "https://api.hubapi.com"


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


async def hubspot_api_request(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a request to the HubSpot API.

    Supports GET, POST, PUT, DELETE, PATCH methods.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = f"{HUBSPOT_BASE_URL}{endpoint}"

    if method.upper() == "GET":
        response = await client.get(url, params=params)
    elif method.upper() == "POST":
        response = await client.post(url, params=params, json=data)
    elif method.upper() == "PUT":
        response = await client.put(url, params=params, json=data)
    elif method.upper() == "PATCH":
        response = await client.patch(url, params=params, json=data)
    elif method.upper() == "DELETE":
        response = await client.delete(url, params=params)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]
