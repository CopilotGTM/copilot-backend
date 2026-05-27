from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers._delta_sync import DeltaSyncResult
from src.workers.integrations_worker.providers.email import outlook_email


def _make_token_info() -> IntegrationTokenInfo:
    return IntegrationTokenInfo(
        access_token="access-token",
        refresh_token="refresh-token",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_fetch_messages_uses_larger_delta_page_size() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[], delta_link="delta-link")),
        ) as paginate_delta,
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = client

        result = await outlook_email.fetch_messages(
            token_info=_make_token_info(),
            connection_id="connection-id",
            is_initial=True,
            last_synced_at=None,
            folder_id="inbox",
        )

    assert result.delta_link == "delta-link"
    assert client_cls.call_args.kwargs["headers"]["Prefer"] == "odata.maxpagesize=500"
    assert paginate_delta.call_args.kwargs["label"] == "Outlook Email"
