"""Tests for Gmail email provider (fetch_labels, fetch_messages, helpers)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.email.normalizer import EmailPayloadNormalizer
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.email.gmail_email import (
    GMAIL_MAILBOX_LABEL_ID,
    FetchMessagesResult,
    _batch_get_messages,
    _extract_latest_history_id,
    _filter_excluded_messages,
    extract_body_from_payload,
    fetch_labels,
    fetch_messages,
    get_header,
)


def _make_token_info() -> IntegrationTokenInfo:
    """Create a valid IntegrationTokenInfo for testing."""
    return IntegrationTokenInfo(
        access_token="test-token-123",
        refresh_token="test-refresh",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        integration_type=IntegrationType.GMAIL,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "https://gmail.googleapis.com/test"),
    )


@pytest.mark.asyncio
async def test_batch_get_messages_propagates_partial_detail_failure() -> None:
    with (
        patch(
            "src.workers.integrations_worker.providers.email.gmail_email.gmail_api_request",
            AsyncMock(side_effect=[{"id": "msg-1"}, RuntimeError("detail failed")]),
        ),
        pytest.raises(RuntimeError, match="detail failed"),
    ):
        await _batch_get_messages(AsyncMock(), ["msg-1", "msg-2"])


# ---------------------------------------------------------------------------
# fetch_labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_labels_returns_system_labels():
    """Labels list + detail calls should return enriched label dicts."""
    token_info = _make_token_info()

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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

    assert len(result) == 3
    assert result[0]["id"] == "INBOX"
    assert result[0]["messagesTotal"] == 120
    assert result[1]["id"] == "SENT"
    assert result[2]["id"] == GMAIL_MAILBOX_LABEL_ID


# ---------------------------------------------------------------------------
# fetch_messages – initial sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_messages_initial_batch_get_details():
    """Initial sync lists IDs then batch-gets full messages."""
    token_info = _make_token_info()

    responses = [
        # profile snapshot anchors the next history pass before the mailbox scan
        _json_response({"historyId": "90"}),
        # messages.list
        _json_response({"messages": [{"id": "msg1"}, {"id": "msg2"}]}),
        # messages.get for msg1
        _json_response(
            {
                "id": "msg1",
                "historyId": "100",
                "labelIds": ["DRAFT"],
                "payload": {"headers": []},
            }
        ),
        # messages.get for msg2
        _json_response(
            {
                "id": "msg2",
                "historyId": "200",
                "labelIds": ["SENT"],
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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    assert isinstance(result, FetchMessagesResult)
    assert len(result.messages) == 2
    assert result.history_id == "90"
    assert [message["_gmail_label_id"] for message in result.messages] == ["DRAFT", "SENT"]


@pytest.mark.asyncio
async def test_fetch_messages_initial_with_date_filter():
    """Initial sync should pass `after:` epoch in query parameter."""
    token_info = _make_token_info()

    responses = [
        _json_response({"historyId": "90"}),
        _json_response({"messages": []}),  # no messages
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = responses
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        await fetch_messages(
            token_info=token_info,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

        call_args = mock_client_instance.get.call_args_list[1]
        params = call_args.kwargs.get("params", {})
        assert "after:" in params.get("q", "")


@pytest.mark.asyncio
async def test_fetch_messages_initial_uses_one_filtered_all_mail_query():
    """The synthetic mailbox pass includes every supported mail class once."""
    token_info = _make_token_info()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = [
            _json_response({"historyId": "90"}),
            _json_response({"messages": []}),
        ]
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        await fetch_messages(
            token_info=token_info,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    params = mock_client_instance.get.call_args_list[1].kwargs["params"]
    query = params["q"]
    assert "labelIds" not in params
    assert params["includeSpamTrash"] is False
    assert "in:anywhere" in query
    assert "-in:sent" not in query
    assert "-in:drafts" not in query
    assert "-in:spam" in query
    assert "-in:trash" in query
    assert "-category:promotions" in query
    assert "-category:social" in query
    assert "-category:forums" in query
    assert "-is:chat" in query
    assert "-category:updates" not in query


# ---------------------------------------------------------------------------
# fetch_messages – incremental sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_messages_incremental_with_history_id():
    """Incremental sync uses history.list and fetches added messages."""
    token_info = _make_token_info()

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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="100",
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    assert len(result.messages) == 1
    assert result.messages[0]["id"] == "msg3"
    assert result.messages[0]["_gmail_label_id"] == "INBOX"
    assert result.history_id == "500"
    history_params = mock_client_instance.get.call_args_list[0].kwargs["params"]
    assert "labelId" not in history_params


@pytest.mark.asyncio
async def test_fetch_messages_incremental_handles_deleted():
    """Deleted messages from history should get _sync_metadata marker."""
    token_info = _make_token_info()

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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="100",
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    deleted = [
        m for m in result.messages if m.get("_sync_metadata", {}).get("sync_action") == "DELETED"
    ]
    assert len(deleted) == 1
    assert deleted[0]["id"] == "msg-deleted"


@pytest.mark.asyncio
async def test_fetch_messages_history_id_expired_fallback():
    """404 on history.list should fallback to full sync."""
    token_info = _make_token_info()

    # 404 response for history.list
    not_found_resp = httpx.Response(
        status_code=404,
        json={"error": {"code": 404, "message": "Not Found"}},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/history"),
    )
    # Fallback full sync responses
    profile_response = _json_response({"historyId": "900"})
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
        """Expire the history request, then serve the full-sync fallback responses."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError(
                "404", request=not_found_resp.request, response=not_found_resp
            )
        elif call_count == 2:
            return profile_response
        elif call_count == 3:
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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="expired-id",
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    assert len(result.messages) == 1
    assert result.messages[0]["id"] == "msg-full"
    assert result.history_id == "900"


@pytest.mark.asyncio
async def test_incremental_label_changes_update_included_mail_and_remove_excluded_mail():
    """Global history keeps archive moves and exclusion transitions consistent."""
    token_info = _make_token_info()
    responses = [
        _json_response(
            {
                "historyId": "700",
                "history": [
                    {
                        "labelsRemoved": [{"message": {"id": "archived"}}],
                        "labelsAdded": [{"message": {"id": "spam"}}],
                    }
                ],
            }
        ),
        _json_response(
            {
                "id": "archived",
                "historyId": "699",
                "labelIds": ["IMPORTANT"],
                "payload": {"headers": []},
            }
        ),
        _json_response(
            {
                "id": "spam",
                "historyId": "700",
                "labelIds": ["SPAM"],
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
            token_info=token_info,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=None,
            history_id="600",
            label_id=GMAIL_MAILBOX_LABEL_ID,
        )

    assert result.history_id == "700"
    assert result.messages[0]["id"] == "archived"
    assert result.messages[0]["_gmail_label_id"] == GMAIL_MAILBOX_LABEL_ID
    assert result.messages[1] == {
        "id": "spam",
        "_gmail_label_id": GMAIL_MAILBOX_LABEL_ID,
        "_sync_metadata": {
            "sync_action": "DELETED",
            "sync_type": "INCREMENTAL",
            "source": "history_excluded",
        },
    }


def test_only_synthetic_gmail_mailbox_folder_is_monitored():
    """One global Gmail cursor must have exactly one scheduling coordinator."""
    normalizer = EmailPayloadNormalizer()

    mailbox = normalizer.extract_gmail_folder_fields(
        {"id": GMAIL_MAILBOX_LABEL_ID, "name": "All Mail"}
    )
    inbox = normalizer.extract_gmail_folder_fields({"id": "INBOX", "name": "Inbox"})
    sent = normalizer.extract_gmail_folder_fields({"id": "SENT", "name": "Sent"})

    assert mailbox["folder_type"] == "archive"
    assert mailbox["is_monitored"] == "true"
    assert inbox["is_monitored"] == "false"
    assert sent["is_monitored"] == "false"


# ---------------------------------------------------------------------------
# _filter_excluded_messages
# ---------------------------------------------------------------------------


def test_filter_excluded_messages():
    """Low-value categories and unsafe system folders are excluded, but Updates remain."""
    messages = [
        {"id": "1", "labelIds": ["INBOX"]},
        {"id": "2", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
        {"id": "3", "labelIds": ["INBOX", "CATEGORY_SOCIAL"]},
        {"id": "4", "labelIds": ["INBOX", "IMPORTANT"]},
        {"id": "5", "_sync_metadata": {"sync_action": "DELETED"}},  # always kept
        {"id": "6", "labelIds": ["CATEGORY_UPDATES"]},
        {"id": "8", "labelIds": ["CHAT"]},
    ]

    filtered, skipped = _filter_excluded_messages(messages)

    assert len(filtered) == 4
    assert skipped == 3
    assert {m["id"] for m in filtered} == {"1", "4", "5", "6"}


def test_filter_keeps_every_included_mail_class():
    """Primary, Updates, drafts, archived/read states, and attachments remain ingestible."""
    messages = [
        {"id": "primary", "labelIds": ["CATEGORY_PERSONAL", "INBOX"]},
        {"id": "updates", "labelIds": ["CATEGORY_UPDATES"]},
        {"id": "draft", "labelIds": ["DRAFT"]},
        {"id": "archived-read", "labelIds": ["IMPORTANT"]},
        {"id": "archived-unread", "labelIds": ["UNREAD"]},
        {"id": "attachment", "labelIds": ["INBOX"], "hasAttachments": True},
    ]

    filtered, skipped = _filter_excluded_messages(messages)

    assert skipped == 0
    assert [message["id"] for message in filtered] == [message["id"] for message in messages]


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
