from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
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


def _batch_response(responses: list[dict]) -> Mock:
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"responses": responses}
    return response


def _full_outlook_message(message_id: str = "msg-1") -> dict:
    return {
        "id": message_id,
        "conversationId": f"thread-{message_id}",
        "subject": "Re: Discussion Summary | Next Steps on POC | Exotel <> CopilotGTM",
        "bodyPreview": "Approved",
        "from": {"emailAddress": {"name": "Suraj", "address": "suraj@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "seller@example.com"}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "replyTo": [],
        "sentDateTime": "2026-07-06T15:53:20Z",
        "receivedDateTime": "2026-07-06T15:53:25Z",
        "hasAttachments": False,
        "importance": "normal",
        "isRead": False,
        "isDraft": False,
        "internetMessageId": f"<{message_id}@mail.gmail.com>",
        "parentFolderId": "inbox",
        "inferenceClassification": "focused",
        "categories": [],
        "body": {"contentType": "text", "content": "Approved."},
    }


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


@pytest.mark.asyncio
async def test_fetch_messages_hydrates_partial_delta_before_body_backfill() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial = {"id": "msg-1", "parentFolderId": "inbox"}
    hydrated = _full_outlook_message("msg-1")

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[partial], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response(
                [
                    {
                        "id": "0",
                        "status": 200,
                        "body": hydrated,
                    }
                ]
            )
        )
        client_cls.return_value = client

        result = await outlook_email.fetch_messages(
            token_info=_make_token_info(),
            connection_id="connection-id",
            is_initial=False,
            last_synced_at=None,
            delta_link="stored-delta",
            folder_id="inbox",
        )

    assert result.messages == [hydrated]
    assert client.post.await_count == 1
    request_url = client.post.await_args.kwargs["json"]["requests"][0]["url"]
    assert "$select=" in request_url
    assert "internetMessageId" in request_url
    assert "body" in request_url


@pytest.mark.asyncio
async def test_fetch_messages_backfills_body_only_for_complete_delta() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    complete_without_body = _full_outlook_message("msg-1")
    complete_without_body.pop("body")

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(
                return_value=DeltaSyncResult(items=[complete_without_body], delta_link="delta-link")
            ),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response(
                [
                    {
                        "id": "0",
                        "status": 200,
                        "body": {
                            "id": "msg-1",
                            "body": {"contentType": "text", "content": "Approved."},
                        },
                    }
                ]
            )
        )
        client_cls.return_value = client

        result = await outlook_email.fetch_messages(
            token_info=_make_token_info(),
            connection_id="connection-id",
            is_initial=False,
            last_synced_at=None,
            delta_link="stored-delta",
            folder_id="inbox",
        )

    assert result.messages[0]["internetMessageId"] == "<msg-1@mail.gmail.com>"
    assert result.messages[0]["body"] == {"contentType": "text", "content": "Approved."}
    request_url = client.post.await_args.kwargs["json"]["requests"][0]["url"]
    assert request_url == "/me/messages/msg-1?$select=id,body"


@pytest.mark.asyncio
async def test_fetch_messages_does_not_hydrate_deleted_delta() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    deleted = {
        "id": "msg-1",
        "@removed": {"reason": "deleted"},
        "_sync_metadata": {"sync_action": "DELETED"},
    }

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[deleted], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock()
        client_cls.return_value = client

        result = await outlook_email.fetch_messages(
            token_info=_make_token_info(),
            connection_id="connection-id",
            is_initial=False,
            last_synced_at=None,
            delta_link="stored-delta",
            folder_id="inbox",
        )

    assert result.messages == [deleted]
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_messages_raises_for_non_deleted_message_without_id() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial_without_id = {"subject": "Sparse update", "parentFolderId": "inbox"}

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(
                return_value=DeltaSyncResult(items=[partial_without_id], delta_link="delta-link")
            ),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock()
        client_cls.return_value = client

        with pytest.raises(RuntimeError, match="non-deleted message missing id"):
            await outlook_email.fetch_messages(
                token_info=_make_token_info(),
                connection_id="connection-id",
                is_initial=False,
                last_synced_at=None,
                delta_link="stored-delta",
                folder_id="inbox",
            )

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_messages_drops_partial_delta_when_hydration_returns_404() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial = {"id": "msg-1", "parentFolderId": "inbox"}

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[partial], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response([{"id": "0", "status": 404, "body": {}}])
        )
        client_cls.return_value = client

        result = await outlook_email.fetch_messages(
            token_info=_make_token_info(),
            connection_id="connection-id",
            is_initial=False,
            last_synced_at=None,
            delta_link="stored-delta",
            folder_id="inbox",
        )

    assert result.messages == []


@pytest.mark.asyncio
async def test_fetch_messages_raises_when_hydration_is_incomplete() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial = {"id": "msg-1", "parentFolderId": "inbox"}
    incomplete_hydration = {
        "id": "msg-1",
        "parentFolderId": "inbox",
        "body": {"contentType": "text", "content": "Approved."},
    }

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[partial], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response([{"id": "0", "status": 200, "body": incomplete_hydration}])
        )
        client_cls.return_value = client

        with pytest.raises(RuntimeError, match="returned incomplete message"):
            await outlook_email.fetch_messages(
                token_info=_make_token_info(),
                connection_id="connection-id",
                is_initial=False,
                last_synced_at=None,
                delta_link="stored-delta",
                folder_id="inbox",
            )


@pytest.mark.asyncio
async def test_fetch_messages_raises_on_non_404_hydration_failure() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial = {"id": "msg-1", "parentFolderId": "inbox"}

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[partial], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response([{"id": "0", "status": 400, "body": {}}])
        )
        client_cls.return_value = client

        with pytest.raises(RuntimeError, match="partial hydration failed"):
            await outlook_email.fetch_messages(
                token_info=_make_token_info(),
                connection_id="connection-id",
                is_initial=False,
                last_synced_at=None,
                delta_link="stored-delta",
                folder_id="inbox",
            )


@pytest.mark.asyncio
async def test_fetch_messages_raises_on_transient_hydration_failure() -> None:
    async def call_request(_token_info, request_fn):
        return await request_fn("access-token")

    partial = {"id": "msg-1", "parentFolderId": "inbox"}

    with (
        patch.object(outlook_email, "call_with_token_retry", side_effect=call_request),
        patch.object(
            outlook_email,
            "paginate_delta",
            AsyncMock(return_value=DeltaSyncResult(items=[partial], delta_link="delta-link")),
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(
            return_value=_batch_response([{"id": "0", "status": 429, "body": {}}])
        )
        client_cls.return_value = client

        with pytest.raises(RuntimeError, match="partial hydration failed"):
            await outlook_email.fetch_messages(
                token_info=_make_token_info(),
                connection_id="connection-id",
                is_initial=False,
                last_synced_at=None,
                delta_link="stored-delta",
                folder_id="inbox",
            )
