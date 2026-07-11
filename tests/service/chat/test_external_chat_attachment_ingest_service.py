"""Attachment-ingest contracts for durable external-chat media and deduplication."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from src.domain.models.organisation_tables.chat_message_models import AttachmentSourceProvider
from src.infra.config.configs import chat_file_upload_config, whatsapp_config
from src.service.bots.whatsapp_bot_service import WhatsAppInboundMedia
from src.service.chat.chat_attachment_manifest import AttachmentContextItem
from src.service.chat.external_chat_attachment_ingest_service import (
    ExternalChatAttachmentIngestService,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        json_data=None,
        body: bytes = b"",
        content_type: str = "application/octet-stream",
    ):
        self._json_data = json_data or {}
        self._body = body
        self.content = _FakeContent(body)
        self.content_type = content_type
        self.status = 200
        self.read_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self):
        return self._json_data

    async def read(self) -> bytes:
        self.read_called = True
        return self._body


class _FakeContent:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, chunk_size: int):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


class _FakeClientSession:
    calls: list[dict[str, object]] = []
    responses: list[_FakeResponse] = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self.responses.pop(0)


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
# ingest_whatsapp_media_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_whatsapp_media_downloads_graph_media_with_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalChatAttachmentIngestService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    captured: dict[str, object] = {}

    async def fake_ingest_attachment_bytes(**kwargs):
        captured.update(kwargs)
        return AttachmentContextItem(
            attachment_id="attachment-1",
            filename=kwargs["filename"],
            mimetype=kwargs["mimetype"],
            source_provider=kwargs["source_provider"].value,
        )

    _FakeClientSession.calls = []
    _FakeClientSession.responses = [
        _FakeResponse(
            json_data={
                "url": "https://lookaside.whatsapp.test/media-1",
                "mime_type": "application/pdf",
                "file_size": 25,
            }
        ),
        _FakeResponse(body=b"%PDF fake", content_type="application/pdf"),
    ]
    monkeypatch.setattr(
        "src.service.chat.external_chat_attachment_ingest_service.aiohttp.ClientSession",
        _FakeClientSession,
    )
    monkeypatch.setattr(
        service,
        "_find_whatsapp_attachment",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(service, "ingest_attachment_bytes", fake_ingest_attachment_bytes)

    items = await service.ingest_whatsapp_media_files(
        [
            WhatsAppInboundMedia(
                media_id="media-1",
                media_type="document",
                mimetype="application/octet-stream",
                filename="proposal.pdf",
                caption="please summarize",
                message_id="wamid.1",
            )
        ],
        access_token="wa-token",
    )

    assert len(items) == 1
    assert items[0].source_provider == "whatsapp"
    assert captured["filename"] == "proposal.pdf"
    assert captured["mimetype"] == "application/pdf"
    assert captured["content"] == b"%PDF fake"
    assert captured["source_provider"] == AttachmentSourceProvider.WHATSAPP
    assert captured["source_external_id"] == "media-1"
    assert _FakeClientSession.calls == [
        {
            "url": f"https://graph.facebook.com/{whatsapp_config.GRAPH_API_VERSION}/media-1",
            "kwargs": {"headers": {"Authorization": "Bearer wa-token"}},
        },
        {
            "url": "https://lookaside.whatsapp.test/media-1",
            "kwargs": {"headers": {"Authorization": "Bearer wa-token"}},
        },
    ]


@pytest.mark.asyncio
async def test_ingest_whatsapp_media_rejects_oversized_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalChatAttachmentIngestService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    async def fail_ingest_attachment_bytes(**_kwargs):
        raise AssertionError("oversized media should not be downloaded or ingested")

    _FakeClientSession.calls = []
    _FakeClientSession.responses = [
        _FakeResponse(
            json_data={
                "url": "https://lookaside.whatsapp.test/media-1",
                "mime_type": "application/pdf",
                "file_size": 100,
            }
        ),
    ]
    monkeypatch.setattr(chat_file_upload_config, "BOT_ATTACHMENT_MAX_INGEST_BYTES", 10)
    monkeypatch.setattr(
        "src.service.chat.external_chat_attachment_ingest_service.aiohttp.ClientSession",
        _FakeClientSession,
    )
    monkeypatch.setattr(
        service,
        "_find_whatsapp_attachment",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(service, "ingest_attachment_bytes", fail_ingest_attachment_bytes)

    from src.service.chat import external_chat_attachment_ingest_service as ingest_module

    with pytest.raises(ingest_module.ExternalChatAttachmentIngestError):
        await service.ingest_whatsapp_media_files(
            [
                WhatsAppInboundMedia(
                    media_id="media-1",
                    media_type="document",
                    mimetype="application/pdf",
                    filename="proposal.pdf",
                    message_id="wamid.1",
                )
            ],
            access_token="wa-token",
        )

    assert len(_FakeClientSession.calls) == 1


@pytest.mark.asyncio
async def test_ingest_whatsapp_media_streams_unknown_size_with_a_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalChatAttachmentIngestService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    download_response = _FakeResponse(
        body=b"01234567890",
        content_type="application/pdf",
    )
    _FakeClientSession.calls = []
    _FakeClientSession.responses = [
        _FakeResponse(
            json_data={
                "url": "https://lookaside.whatsapp.test/media-1",
                "mime_type": "application/pdf",
                "file_size": 0,
            }
        ),
        download_response,
    ]
    monkeypatch.setattr(chat_file_upload_config, "BOT_ATTACHMENT_MAX_INGEST_BYTES", 10)
    monkeypatch.setattr(
        "src.service.chat.external_chat_attachment_ingest_service.aiohttp.ClientSession",
        _FakeClientSession,
    )
    monkeypatch.setattr(service, "_find_whatsapp_attachment", AsyncMock(return_value=None))
    ingest_mock = AsyncMock()
    monkeypatch.setattr(service, "ingest_attachment_bytes", ingest_mock)

    from src.service.chat import external_chat_attachment_ingest_service as ingest_module

    with pytest.raises(ingest_module.ExternalChatAttachmentIngestError):
        await service.ingest_whatsapp_media_files(
            [
                WhatsAppInboundMedia(
                    media_id="media-1",
                    media_type="document",
                    mimetype="application/pdf",
                    filename="proposal.pdf",
                    message_id="wamid.1",
                )
            ],
            access_token="wa-token",
        )

    assert download_response.read_called is False
    ingest_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_whatsapp_media_reuses_existing_attachment_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExternalChatAttachmentIngestService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    existing = SimpleNamespace(
        attachment_id=uuid4(),
        filename="voice.ogg",
        mimetype="audio/ogg",
        source_provider=AttachmentSourceProvider.WHATSAPP,
    )
    monkeypatch.setattr(
        service,
        "_find_whatsapp_attachment",
        AsyncMock(return_value=existing),
        raising=False,
    )
    _FakeClientSession.calls = []
    _FakeClientSession.responses = []
    monkeypatch.setattr(
        "src.service.chat.external_chat_attachment_ingest_service.aiohttp.ClientSession",
        _FakeClientSession,
    )

    items = await service.ingest_whatsapp_media_files(
        [
            WhatsAppInboundMedia(
                media_id="media-voice",
                media_type="audio",
                mimetype="audio/ogg",
                filename="voice.ogg",
                message_id="wamid.1",
            )
        ],
        access_token="wa-token",
    )

    assert [item.attachment_id for item in items] == [str(existing.attachment_id)]
    assert _FakeClientSession.calls == []


@pytest.mark.asyncio
async def test_whatsapp_attachment_unique_race_deletes_losing_blob_and_reuses_winner() -> None:
    winner = SimpleNamespace(
        attachment_id=uuid4(),
        filename="proposal.pdf",
        mimetype="application/pdf",
        source_provider=AttachmentSourceProvider.WHATSAPP,
    )

    class _RaceResult:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return SimpleNamespace(first=lambda: self.value)

    class _RaceSession:
        def __init__(self):
            self.results = iter([None, winner])
            self.rollbacks = 0

        async def execute(self, _statement):
            return _RaceResult(next(self.results))

        def add(self, _value):
            return None

        async def flush(self):
            raise IntegrityError("insert", {}, RuntimeError("duplicate"))

        async def rollback(self):
            self.rollbacks += 1

    class _Storage:
        def __init__(self):
            self.deleted: list[str] = []

        def plan_blob_path(self, **_kwargs):
            return "attachments/losing-copy.pdf"

        async def upload_bytes(self, *_args, **_kwargs):
            return None

        async def delete_blob(self, blob_path: str):
            self.deleted.append(blob_path)

    session = _RaceSession()
    service = ExternalChatAttachmentIngestService(
        org_session=session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    storage = _Storage()
    service.storage = storage

    result = await service.ingest_attachment_bytes(
        filename="proposal.pdf",
        mimetype="application/pdf",
        content=b"pdf",
        source_provider=AttachmentSourceProvider.WHATSAPP,
        source_external_id="media-1",
    )

    assert result.attachment_id == str(winner.attachment_id)
    assert session.rollbacks == 1
    assert storage.deleted == ["attachments/losing-copy.pdf"]


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
