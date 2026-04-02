"""HubSpot direct API base fetcher.

Overrides BaseFetcher._make_request() to bypass Nango proxy and call
the HubSpot API directly via httpx. Nango is still used for OAuth token
management (storage + refresh).
"""

from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.nango_client import NangoClient, NangoConnectionError
from src.workers.integrations_worker.core.base_fetcher import BaseFetcher
from src.workers.integrations_worker.providers.crm._hubspot_api import (
    get_access_token,
    hubspot_api_request,
)


class HubSpotDirectFetcher(BaseFetcher):
    """BaseFetcher subclass that uses direct HubSpot API calls instead of Nango proxy."""

    def __init__(
        self,
        nango_client: NangoClient,
        provider_config_key: str,
        connection_id: str,
    ) -> None:
        super().__init__(nango_client, provider_config_key, connection_id)
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> None:
        if not self._client:
            self._token = await get_access_token(
                self.nango_client, self.connection_id, self.provider_config_key
            )
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=30.0,
            )

    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request, wrapping httpx errors as NangoConnectionError.

        BaseFetcher._fetch_paginated/_fetch_batch catch NangoConnectionError
        to detect 401/403 and raise TokenExpiredError. We preserve that
        contract by wrapping httpx errors the same way nango_client.proxy_request did.
        """
        await self._ensure_client()
        assert self._client is not None  # noqa: S101
        try:
            return await hubspot_api_request(
                self._client, endpoint, method=method, params=params, data=data
            )
        except httpx.HTTPStatusError as exc:
            raise NangoConnectionError(
                str(exc),
                status_code=exc.response.status_code,
                payload=exc.response.text,
            ) from exc
        except httpx.HTTPError as exc:
            raise NangoConnectionError(str(exc)) from exc

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
