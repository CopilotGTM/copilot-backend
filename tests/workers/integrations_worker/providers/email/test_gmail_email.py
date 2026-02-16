"""Tests for Gmail email provider (fetch_labels, fetch_messages, helpers)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from src.workers.integrations_worker.providers.email.gmail_email import (
    FetchMessagesResult,
    _extract_latest_history_id,
    _filter_category_messages,
    extract_body_from_payload,
    fetch_labels,
    fetch_messages,
    get_header,
)


def _mock_nango_with_token() -> AsyncMock:
    """Create a NangoClient mock that returns a valid access token."""
    nango = AsyncMock()
    nango.get_connection.return_value = {
        "credentials": {"access_token": "test-token-123"},
    }
    return nango


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "https://gmail.googleapis.com/test"),
    )


# ---------------------------------------------------------------------------
# fetch_labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_labels_returns_system_labels():
    """Labels list + detail calls should return enriched label dicts."""
    nango = _mock_nango_with_token()

    responses = [
        # labels.list
        _json_response(
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX"},
                    {"id": "SENT", "name": "SENT"},
                ]
            }
        ),
        # labels.get for INBOX
        _json_response(
            {
                "id": "INBOX",
                "name": "INBOX",
                "type": "system",
                "messagesTotal": 120,
                "messagesUnread": 5,
            }
        ),
        # labels.get for SENT
        _json_response(
            {
                "id": "SENT",
                "name": "SENT",
                "type": "system",
                "messagesTotal": 80,
                "messagesUnread": 0,
            }
        ),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = await fetch_labels(
            nango=nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

    assert len(result) == 2
    assert result[0]["id"] == "INBOX"
    assert result[0]["messagesTotal"] == 120
    assert result[1]["id"] == "SENT"


# ---------------------------------------------------------------------------
# fetch_messages – initial sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_messages_initial_batch_get_details():
    """Initial sync lists IDs then batch-gets full messages."""
    nango = _mock_nango_with_token()

    responses = [
        # messages.list
        _json_response({"messages": [{"id": "msg1"}, {"id": "msg2"}]}),
        # messages.get for msg1
        _json_response(
            {
                "id": "msg1",
                "historyId": "100",
                "labelIds": ["INBOX"],
                "payload": {"headers": []},
            }
        ),
        # messages.get for msg2
        _json_response(
            {
                "id": "msg2",
                "historyId": "200",
                "labelIds": ["INBOX"],
                "payload": {"headers": []},
            }
        ),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = await fetch_messages(
            nango=nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            label_id="INBOX",
        )

    assert isinstance(result, FetchMessagesResult)
    assert len(result.messages) == 2
    assert result.history_id == "200"  # highest


@pytest.mark.asyncio
async def test_fetch_messages_initial_with_date_filter():
    """Initial sync should pass `after:` epoch in query parameter."""
    nango = _mock_nango_with_token()

    responses = [
        _json_response({"messages": []}),  # no messages
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        await fetch_messages(
            nango=nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            label_id="INBOX",
        )

        # First call should be messages.list with `q=after:...`
        call_args = mock_client_instance.get.call_args_list[0]
        params = call_args.kwargs.get("params", {})
        assert "after:" in params.get("q", "")


# ---------------------------------------------------------------------------
# fetch_messages – incremental sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_messages_incremental_with_history_id():
    """Incremental sync uses history.list and fetches added messages."""
    nango = _mock_nango_with_token()

    responses = [
        # history.list
        _json_response(
            {
                "historyId": "500",
                "history": [
                    {
                        "messagesAdded": [{"message": {"id": "msg3"}}],
                        "messagesDeleted": [],
                    },
                ],
            }
        ),
        # messages.get for msg3
        _json_response(
            {
                "id": "msg3",
                "historyId": "500",
                "labelIds": ["INBOX"],
                "payload": {"headers": []},
            }
        ),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = await fetch_messages(
            nango=nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="100",
            label_id="INBOX",
        )

    assert len(result.messages) == 1
    assert result.messages[0]["id"] == "msg3"
    assert result.history_id == "500"


@pytest.mark.asyncio
async def test_fetch_messages_incremental_handles_deleted():
    """Deleted messages from history should get _sync_metadata marker."""
    nango = _mock_nango_with_token()

    responses = [
        _json_response(
            {
                "historyId": "600",
                "history": [
                    {
                        "messagesAdded": [],
                        "messagesDeleted": [{"message": {"id": "msg-deleted"}}],
                    },
                ],
            }
        ),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = await fetch_messages(
            nango=nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="100",
            label_id="INBOX",
        )

    deleted = [
        m for m in result.messages if m.get("_sync_metadata", {}).get("sync_action") == "DELETED"
    ]
    assert len(deleted) == 1
    assert deleted[0]["id"] == "msg-deleted"


@pytest.mark.asyncio
async def test_fetch_messages_history_id_expired_fallback():
    """404 on history.list should fallback to full sync."""
    nango = _mock_nango_with_token()

    # 404 response for history.list
    not_found_resp = httpx.Response(
        status_code=404,
        json={"error": {"code": 404, "message": "Not Found"}},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/history"),
    )
    # Fallback full sync responses
    list_response = _json_response({"messages": [{"id": "msg-full"}]})
    get_response = _json_response(
        {
            "id": "msg-full",
            "historyId": "999",
            "labelIds": ["INBOX"],
            "payload": {"headers": []},
        }
    )

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError(
                "404", request=not_found_resp.request, response=not_found_resp
            )
        elif call_count == 2:
            return list_response
        else:
            return get_response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = mock_get
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = await fetch_messages(
            nango=nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="expired-id",
            label_id="INBOX",
        )

    assert len(result.messages) == 1
    assert result.messages[0]["id"] == "msg-full"


# ---------------------------------------------------------------------------
# _filter_category_messages
# ---------------------------------------------------------------------------


def test_filter_category_messages():
    """Should filter CATEGORY_PROMOTIONS etc. but keep normal messages."""
    messages = [
        {"id": "1", "labelIds": ["INBOX"]},
        {"id": "2", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
        {"id": "3", "labelIds": ["INBOX", "CATEGORY_SOCIAL"]},
        {"id": "4", "labelIds": ["INBOX", "IMPORTANT"]},
        {"id": "5", "_sync_metadata": {"sync_action": "DELETED"}},  # always kept
    ]

    filtered, skipped = _filter_category_messages(messages)

    assert len(filtered) == 3
    assert skipped == 2
    assert {m["id"] for m in filtered} == {"1", "4", "5"}


# ---------------------------------------------------------------------------
# extract_body_from_payload
# ---------------------------------------------------------------------------


def test_extract_body_from_simple_payload():
    """Simple text/plain payload."""
    import base64

    text = "Hello world"
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    payload = {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }

    body_text, body_html = extract_body_from_payload(payload)
    assert body_text == "Hello world"
    assert body_html is None


def test_extract_body_from_multipart_payload():
    """Multipart payload with text and HTML parts."""
    import base64

    text = "Plain text"
    html = "<p>HTML content</p>"
    text_encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    html_encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")

    payload = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": text_encoded}},
            {"mimeType": "text/html", "body": {"data": html_encoded}},
        ],
    }

    body_text, body_html = extract_body_from_payload(payload)
    assert body_text == "Plain text"
    assert body_html == "<p>HTML content</p>"


# ---------------------------------------------------------------------------
# get_header
# ---------------------------------------------------------------------------


def test_extract_header_case_insensitive():
    """Header lookup should be case-insensitive."""
    headers = [
        {"name": "Subject", "value": "Test Email"},
        {"name": "from", "value": "alice@example.com"},
        {"name": "Message-ID", "value": "<abc@mail.example.com>"},
    ]

    assert get_header(headers, "subject") == "Test Email"
    assert get_header(headers, "FROM") == "alice@example.com"
    assert get_header(headers, "message-id") == "<abc@mail.example.com>"
    assert get_header(headers, "nonexistent") is None


# ---------------------------------------------------------------------------
# _extract_latest_history_id
# ---------------------------------------------------------------------------


def test_extract_latest_history_id():
    """Should return the highest historyId as string."""
    messages = [
        {"historyId": "100"},
        {"historyId": "500"},
        {"historyId": "200"},
    ]
    assert _extract_latest_history_id(messages) == "500"


def test_extract_latest_history_id_empty():
    """Should return None for empty list."""
    assert _extract_latest_history_id([]) is None
