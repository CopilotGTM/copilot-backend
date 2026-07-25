"""Tests for the email attachment downloader provider."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from src.workers.integrations_worker.providers.email.attachment_downloader import (
    AttachmentMeta,
    DownloadedAttachment,
    download_gmail_attachment,
    download_outlook_attachment,
    filter_junk_attachments,
    list_gmail_attachments,
    list_outlook_attachments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_attachment(
    filename: str = "document.pdf",
    mime_type: str = "application/pdf",
    size_bytes: int = 50_000,
    attachment_id: str = "att-1",
) -> AttachmentMeta:
    return AttachmentMeta(
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        attachment_id=attachment_id,
    )


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "https://example.com/test"),
    )


# ---------------------------------------------------------------------------
# TestFilterJunkAttachments
# ---------------------------------------------------------------------------


class TestFilterJunkAttachments:
    def test_keeps_normal_pdf(self):
        attachments = [_make_attachment("report.pdf", "application/pdf", 200_000)]
        result = filter_junk_attachments(attachments)
        assert len(result) == 1
        assert result[0].filename == "report.pdf"

    def test_skips_oversized_attachment(self):
        # 26 MB — exceeds 25 MB limit
        attachments = [_make_attachment("huge.pdf", "application/pdf", 26 * 1024 * 1024)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_tiny_attachment(self):
        # 500 bytes — tracking pixel territory
        attachments = [_make_attachment("pixel.png", "image/png", 500)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_ics_by_extension(self):
        attachments = [_make_attachment("invite.ics", "text/plain", 5_000)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_ics_by_mime_type(self):
        attachments = [_make_attachment("invite.dat", "text/calendar", 5_000)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_ics_by_mime_type_case_insensitive(self):
        attachments = [_make_attachment("invite.dat", "Text/Calendar", 5_000)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_eml_by_extension(self):
        attachments = [_make_attachment("forwarded.eml", "message/rfc822", 50_000)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_eml_by_mime_type(self):
        attachments = [_make_attachment("email_thread", "message/rfc822", 50_000)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_small_inline_image(self):
        # 50 KB image — under 100 KB threshold
        attachments = [_make_attachment("logo.png", "image/png", 50 * 1024)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_keeps_large_image(self):
        # 150 KB image — over 100 KB threshold, should be kept
        attachments = [_make_attachment("screenshot.png", "image/png", 150 * 1024)]
        result = filter_junk_attachments(attachments)
        assert len(result) == 1

    def test_skips_signature_image_pattern(self):
        # image001.png pattern — signature images regardless of size
        attachments = [_make_attachment("image001.png", "image/png", 500 * 1024)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_signature_image_pattern_case_insensitive(self):
        attachments = [_make_attachment("IMAGE002.PNG", "image/png", 500 * 1024)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_skips_signature_image_various_extensions(self):
        attachments = [_make_attachment("image003.jpg", "image/jpeg", 200 * 1024)]
        result = filter_junk_attachments(attachments)
        assert result == []

    def test_mixed_batch_filters_correctly(self):
        attachments = [
            _make_attachment("report.pdf", "application/pdf", 200_000),  # keep
            _make_attachment("huge.zip", "application/zip", 30 * 1024 * 1024),  # skip: oversized
            _make_attachment("pixel.gif", "image/gif", 300),  # skip: tiny
            _make_attachment("meeting.ics", "text/calendar", 2_000),  # skip: calendar
            _make_attachment("image001.png", "image/png", 800_000),  # skip: signature
            _make_attachment("screenshot.png", "image/png", 200 * 1024),  # keep: large image
            _make_attachment("logo.svg", "image/svg+xml", 10 * 1024),  # skip: small image
        ]
        result = filter_junk_attachments(attachments)
        filenames = {a.filename for a in result}
        assert filenames == {"report.pdf", "screenshot.png"}

    def test_boundary_exactly_25mb_is_kept(self):
        # Exactly 25 MB should still be kept (limit is > 25 MB)
        attachments = [_make_attachment("border.pdf", "application/pdf", 25 * 1024 * 1024)]
        result = filter_junk_attachments(attachments)
        assert len(result) == 1

    def test_boundary_exactly_1kb_is_kept(self):
        # Exactly 1 KB should be kept (limit is < 1 KB)
        attachments = [_make_attachment("tiny.pdf", "application/pdf", 1024)]
        result = filter_junk_attachments(attachments)
        assert len(result) == 1

    def test_empty_list_returns_empty(self):
        assert filter_junk_attachments([]) == []


# ---------------------------------------------------------------------------
# TestDownloadGmailAttachment
# ---------------------------------------------------------------------------


class TestDownloadGmailAttachment:
    @pytest.mark.asyncio
    async def test_downloads_and_decodes_base64url(self):
        content = b"Hello, this is attachment content"
        # Gmail uses URL-safe base64
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        result = await download_gmail_attachment(mock_client, "msg-1", "att-1")

        assert isinstance(result, DownloadedAttachment)
        assert result.content == content
        assert result.size_bytes == len(content)

    @pytest.mark.asyncio
    async def test_url_contains_message_and_attachment_ids(self):
        content = b"data"
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        await download_gmail_attachment(mock_client, "msg-abc", "att-xyz")

        call_url = mock_client.get.call_args[0][0]
        assert "msg-abc" in call_url
        assert "att-xyz" in call_url

    @pytest.mark.asyncio
    async def test_url_encodes_path_ids(self):
        content = b"data"
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        await download_gmail_attachment(mock_client, "msg/abc", "att?xyz")

        call_url = mock_client.get.call_args[0][0]
        assert "msg%2Fabc" in call_url
        assert "att%3Fxyz" in call_url
        assert "msg/abc" not in call_url

    @pytest.mark.asyncio
    async def test_uses_gmail_base_url(self):
        content = b"data"
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        await download_gmail_attachment(mock_client, "msg-1", "att-1")

        call_url = mock_client.get.call_args[0][0]
        assert "gmail.googleapis.com" in call_url

    @pytest.mark.asyncio
    async def test_handles_url_safe_base64_characters(self):
        # Content that encodes to URL-safe chars (- and _)
        content = bytes(range(256))
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        result = await download_gmail_attachment(mock_client, "msg-1", "att-1")
        assert result.content == content

    @pytest.mark.asyncio
    async def test_rejects_decoded_content_over_size_limit(self):
        content = b"x" * (25 * 1024 * 1024 + 1)
        encoded = base64.urlsafe_b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": encoded})

        with pytest.raises(ValueError, match="exceeds"):
            await download_gmail_attachment(mock_client, "msg-1", "att-1")

    @pytest.mark.asyncio
    async def test_rejects_malformed_base64(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"data": "not valid @@@ base64"})

        with pytest.raises(ValueError, match="Invalid Gmail attachment encoding"):
            await download_gmail_attachment(mock_client, "msg-1", "att-1")


# ---------------------------------------------------------------------------
# TestDownloadOutlookAttachment
# ---------------------------------------------------------------------------


class TestDownloadOutlookAttachment:
    @pytest.mark.asyncio
    async def test_downloads_and_decodes_standard_base64(self):
        content = b"Outlook attachment content here"
        # Outlook uses standard base64
        encoded = base64.b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": encoded})

        result = await download_outlook_attachment(mock_client, "msg-1", "att-1")

        assert isinstance(result, DownloadedAttachment)
        assert result.content == content
        assert result.size_bytes == len(content)

    @pytest.mark.asyncio
    async def test_url_contains_message_and_attachment_ids(self):
        content = b"data"
        encoded = base64.b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": encoded})

        await download_outlook_attachment(mock_client, "msg-xyz", "att-abc")

        call_url = mock_client.get.call_args[0][0]
        assert "msg-xyz" in call_url
        assert "att-abc" in call_url

    @pytest.mark.asyncio
    async def test_url_encodes_path_ids(self):
        content = b"data"
        encoded = base64.b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": encoded})

        await download_outlook_attachment(mock_client, "msg/xyz", "att?abc")

        call_url = mock_client.get.call_args[0][0]
        assert "msg%2Fxyz" in call_url
        assert "att%3Fabc" in call_url
        assert "msg/xyz" not in call_url

    @pytest.mark.asyncio
    async def test_uses_graph_base_url(self):
        content = b"data"
        encoded = base64.b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": encoded})

        await download_outlook_attachment(mock_client, "msg-1", "att-1")

        call_url = mock_client.get.call_args[0][0]
        assert "graph.microsoft.com" in call_url

    @pytest.mark.asyncio
    async def test_rejects_decoded_content_over_size_limit(self):
        content = b"x" * (25 * 1024 * 1024 + 1)
        encoded = base64.b64encode(content).decode()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": encoded})

        with pytest.raises(ValueError, match="exceeds"):
            await download_outlook_attachment(mock_client, "msg-1", "att-1")

    @pytest.mark.asyncio
    async def test_rejects_malformed_base64(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"contentBytes": "not valid @@@ base64"})

        with pytest.raises(ValueError, match="Invalid Outlook attachment encoding"):
            await download_outlook_attachment(mock_client, "msg-1", "att-1")


# ---------------------------------------------------------------------------
# TestListGmailAttachments
# ---------------------------------------------------------------------------


class TestListGmailAttachments:
    def test_extracts_attachment_from_parts(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": "SGVsbG8="},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "contract.pdf",
                    "body": {
                        "attachmentId": "att-123",
                        "size": 45_000,
                    },
                },
            ],
        }

        result = list_gmail_attachments(payload)

        assert len(result) == 1
        assert result[0].filename == "contract.pdf"
        assert result[0].mime_type == "application/pdf"
        assert result[0].size_bytes == 45_000
        assert result[0].attachment_id == "att-123"

    def test_returns_empty_for_payload_with_no_attachments(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8="},
        }
        result = list_gmail_attachments(payload)
        assert result == []

    def test_skips_filename_less_body_stored_with_attachment_id(self):
        """Gmail can externalize a large body part without making it a file attachment."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"attachmentId": "body-1", "size": 5_000},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "contract.pdf",
                    "body": {"attachmentId": "att-1", "size": 5_000},
                },
            ],
        }

        result = list_gmail_attachments(payload)

        assert [item.attachment_id for item in result] == ["att-1"]

    def test_returns_empty_for_empty_payload(self):
        result = list_gmail_attachments({})
        assert result == []

    def test_extracts_from_nested_multipart(self):
        """Nested multipart/alternative inside multipart/mixed should recurse correctly."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}},
                        {"mimeType": "text/html", "body": {"data": "PHA+SGVsbG88L3A+"}},
                    ],
                },
                {
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "filename": "data.xlsx",
                    "body": {
                        "attachmentId": "att-456",
                        "size": 120_000,
                    },
                },
            ],
        }

        result = list_gmail_attachments(payload)

        assert len(result) == 1
        assert result[0].filename == "data.xlsx"
        assert result[0].attachment_id == "att-456"

    def test_skips_parts_without_attachment_id(self):
        """Parts with no attachmentId (inline body data) should be ignored."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "filename": "fake.pdf",
                    "body": {"data": "SGVsbG8="},  # inline data, no attachmentId
                },
            ],
        }
        result = list_gmail_attachments(payload)
        assert result == []

    def test_multiple_attachments(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}},
                {
                    "mimeType": "application/pdf",
                    "filename": "doc1.pdf",
                    "body": {"attachmentId": "att-1", "size": 10_000},
                },
                {
                    "mimeType": "image/png",
                    "filename": "image.png",
                    "body": {"attachmentId": "att-2", "size": 5_000},
                },
            ],
        }

        result = list_gmail_attachments(payload)
        assert len(result) == 2
        filenames = {a.filename for a in result}
        assert filenames == {"doc1.pdf", "image.png"}

    def test_ignores_malformed_child_parts(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                "not-a-dict",
                None,
                {
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "att-1", "size": 10_000},
                },
            ],
        }

        result = list_gmail_attachments(payload)

        assert len(result) == 1
        assert result[0].filename == "doc.pdf"

    def test_coerces_malformed_size_to_zero(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "att-1", "size": "not-a-number"},
                },
            ],
        }

        result = list_gmail_attachments(payload)

        assert len(result) == 1
        assert result[0].size_bytes == 0


# ---------------------------------------------------------------------------
# TestListOutlookAttachments
# ---------------------------------------------------------------------------


class TestListOutlookAttachments:
    @pytest.mark.asyncio
    async def test_returns_file_attachments_only(self):
        """Only #microsoft.graph.fileAttachment should be returned."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att-1",
                        "name": "proposal.pdf",
                        "contentType": "application/pdf",
                        "size": 80_000,
                    },
                    {
                        "@odata.type": "#microsoft.graph.itemAttachment",
                        "id": "att-2",
                        "name": "Forwarded Email",
                        "contentType": "message/rfc822",
                        "size": 5_000,
                    },
                ]
            }
        )

        result = await list_outlook_attachments(mock_client, "msg-1")

        assert len(result) == 1
        assert result[0].filename == "proposal.pdf"
        assert result[0].attachment_id == "att-1"
        assert result[0].mime_type == "application/pdf"
        assert result[0].size_bytes == 80_000

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_attachments(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"value": []})

        result = await list_outlook_attachments(mock_client, "msg-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_item_attachments(self):
        """itemAttachment types (forwarded emails) must be excluded."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.itemAttachment",
                        "id": "att-3",
                        "name": "Embedded Message",
                        "contentType": "message/rfc822",
                        "size": 3_000,
                    },
                ]
            }
        )

        result = await list_outlook_attachments(mock_client, "msg-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_file_attachment_without_id(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "proposal.pdf",
                        "contentType": "application/pdf",
                        "size": 80_000,
                    },
                ]
            }
        )

        result = await list_outlook_attachments(mock_client, "msg-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_coerces_malformed_outlook_size_to_zero(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att-1",
                        "name": "proposal.pdf",
                        "contentType": "application/pdf",
                        "size": "not-a-number",
                    },
                ]
            }
        )

        result = await list_outlook_attachments(mock_client, "msg-1")

        assert len(result) == 1
        assert result[0].size_bytes == 0

    @pytest.mark.asyncio
    async def test_uses_select_query_params(self):
        """Graph API call should use $select to limit returned fields."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"value": []})

        await list_outlook_attachments(mock_client, "msg-1")

        call_kwargs = mock_client.get.call_args
        # Either positional or keyword params
        url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("url", "")
        params = call_kwargs[1].get("params") or {}
        # $select should be present either in URL or params
        full_url = url + str(params)
        assert "$select" in full_url or "select" in full_url.lower()

    @pytest.mark.asyncio
    async def test_url_contains_message_id(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"value": []})

        await list_outlook_attachments(mock_client, "msg-unique-123")

        call_url = mock_client.get.call_args[0][0]
        assert "msg-unique-123" in call_url

    @pytest.mark.asyncio
    async def test_url_encodes_message_id(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _json_response({"value": []})

        await list_outlook_attachments(mock_client, "msg/unique?123")

        call_url = mock_client.get.call_args[0][0]
        assert "msg%2Funique%3F123" in call_url
        assert "msg/unique?123" not in call_url
