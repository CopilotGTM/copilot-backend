from __future__ import annotations

import base64

from src.service.chat.external_chat_attachment_ingest_service import (
    ExternalChatAttachmentIngestService,
)

# ---------------------------------------------------------------------------
# _resolve_teams_download — static method, no setup needed
# ---------------------------------------------------------------------------


def test_resolve_teams_download_upload_source_returns_url_directly() -> None:
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="upload",
        download_url="https://example.com/file.pdf",
        bot_token="bot-tok",
        graph_token="graph-tok",
    )
    assert url == "https://example.com/file.pdf"
    assert headers == {}


def test_resolve_teams_download_inline_source_uses_bot_token() -> None:
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="inline",
        download_url="https://smba.trafficmanager.net/file.pdf",
        bot_token="bot-tok",
        graph_token=None,
    )
    assert url == "https://smba.trafficmanager.net/file.pdf"
    assert headers == {"Authorization": "Bearer bot-tok"}


def test_resolve_teams_download_inline_source_no_token_returns_none() -> None:
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="inline",
        download_url="https://smba.trafficmanager.net/file.pdf",
        bot_token=None,
        graph_token=None,
    )
    assert url is None
    assert headers == {}


def test_resolve_teams_download_onedrive_source_builds_graph_url() -> None:
    download_url = "https://example.sharepoint.com/file.pdf"
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="onedrive",
        download_url=download_url,
        bot_token=None,
        graph_token="graph-tok",
    )
    encoded = base64.urlsafe_b64encode(download_url.encode()).decode().rstrip("=")
    expected_url = f"https://graph.microsoft.com/v1.0/shares/u!{encoded}/driveItem/content"
    assert url == expected_url
    assert headers == {"Authorization": "Bearer graph-tok"}


def test_resolve_teams_download_onedrive_no_graph_token_returns_none() -> None:
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="onedrive",
        download_url="https://example.sharepoint.com/file.pdf",
        bot_token=None,
        graph_token=None,
    )
    assert url is None
    assert headers == {}


def test_resolve_teams_download_unknown_source_returns_url_directly() -> None:
    url, headers = ExternalChatAttachmentIngestService._resolve_teams_download(
        source="unknown",
        download_url="https://example.com/file.pdf",
        bot_token=None,
        graph_token=None,
    )
    assert url == "https://example.com/file.pdf"
    assert headers == {}


# ---------------------------------------------------------------------------
# _guess_mimetype — static method
# ---------------------------------------------------------------------------


def test_guess_mimetype_prefers_response_content_type() -> None:
    result = ExternalChatAttachmentIngestService._guess_mimetype(
        "file.txt", "txt", "application/pdf"
    )
    assert result == "application/pdf"


def test_guess_mimetype_ignores_octet_stream_response() -> None:
    result = ExternalChatAttachmentIngestService._guess_mimetype(
        "report.pdf", "pdf", "application/octet-stream"
    )
    assert result == "application/pdf"


def test_guess_mimetype_uses_filename_extension() -> None:
    result = ExternalChatAttachmentIngestService._guess_mimetype("image.png", "", "")
    assert result == "image/png"


def test_guess_mimetype_falls_back_to_file_type_ext_map() -> None:
    result = ExternalChatAttachmentIngestService._guess_mimetype("noext", "xlsx", "")
    assert result == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_guess_mimetype_returns_octet_stream_for_unknown() -> None:
    result = ExternalChatAttachmentIngestService._guess_mimetype("noext", "unknown", "")
    assert result == "application/octet-stream"


def test_guess_mimetype_audio_formats() -> None:
    assert "audio" in ExternalChatAttachmentIngestService._guess_mimetype("a.mp3", "mp3", "")
    assert "audio" in ExternalChatAttachmentIngestService._guess_mimetype("a.wav", "wav", "")
    assert "audio" in ExternalChatAttachmentIngestService._guess_mimetype("a.m4a", "m4a", "")


def test_guess_mimetype_image_formats() -> None:
    assert "image" in ExternalChatAttachmentIngestService._guess_mimetype("i.jpg", "jpg", "")
    assert "image" in ExternalChatAttachmentIngestService._guess_mimetype("i.jpeg", "jpeg", "")
    assert "image" in ExternalChatAttachmentIngestService._guess_mimetype("i.gif", "gif", "")
    assert "image" in ExternalChatAttachmentIngestService._guess_mimetype("i.webp", "webp", "")
