from __future__ import annotations

import httpx
import pytest
from src.infra.media.audio_transcription import (
    _build_assemblyai_url,
    _build_transcription_url,
    _extract_transcript,
    transcribe_audio,
)


def _patch_assemblyai_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASR_PROVIDER",
        "assemblyai",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_API_KEY",
        "assembly-key",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config._assemblyai_api_key_from_env",
        True,
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_BASE_URL",
        "https://api.assemblyai.com",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_SPEECH_MODEL",
        "universal-3-5-pro",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_LANGUAGE_DETECTION",
        True,
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_MAX_POLL_ATTEMPTS",
        2,
    )


# ---------------------------------------------------------------------------
# _build_assemblyai_url
# ---------------------------------------------------------------------------


def test_build_assemblyai_url_strips_trailing_slash() -> None:
    assert _build_assemblyai_url("https://api.assemblyai.com/", "/v2/upload") == (
        "https://api.assemblyai.com/v2/upload"
    )


# ---------------------------------------------------------------------------
# _build_transcription_url
# ---------------------------------------------------------------------------


def test_build_transcription_url_avoids_double_openai_segment() -> None:
    assert _build_transcription_url(
        "https://example.openai.azure.com/openai",
        "gpt-4o-transcribe",
        "2025-03-01-preview",
    ) == (
        "https://example.openai.azure.com/openai/deployments/gpt-4o-transcribe/"
        "audio/transcriptions?api-version=2025-03-01-preview"
    )


def test_build_transcription_url_appends_openai_when_missing() -> None:
    result = _build_transcription_url(
        "https://example.openai.azure.com",
        "gpt-4o-transcribe",
        "2025-03-01-preview",
    )
    assert result == (
        "https://example.openai.azure.com/openai/deployments/gpt-4o-transcribe/"
        "audio/transcriptions?api-version=2025-03-01-preview"
    )


def test_build_transcription_url_strips_trailing_slash() -> None:
    result = _build_transcription_url(
        "https://example.openai.azure.com/",
        "gpt-4o-transcribe",
        "2025-03-01-preview",
    )
    assert "/openai/deployments/" in result
    assert "//" not in result.replace("https://", "")


# ---------------------------------------------------------------------------
# _extract_transcript
# ---------------------------------------------------------------------------


def test_extract_transcript_json_response() -> None:
    response = httpx.Response(
        200,
        json={"text": "  hello world  "},
        headers={"content-type": "application/json"},
    )
    assert _extract_transcript(response) == "hello world"


def test_extract_transcript_plain_text_response() -> None:
    response = httpx.Response(
        200,
        text="  plain text transcript  ",
        headers={"content-type": "text/plain"},
    )
    assert _extract_transcript(response) == "plain text transcript"


def test_extract_transcript_json_missing_text_key() -> None:
    response = httpx.Response(
        200,
        json={"result": "something"},
        headers={"content-type": "application/json"},
    )
    assert _extract_transcript(response) == ""


def test_extract_transcript_json_non_dict_payload() -> None:
    response = httpx.Response(
        200,
        json=["not", "a", "dict"],
        headers={"content-type": "application/json"},
    )
    assert _extract_transcript(response) == ""


# ---------------------------------------------------------------------------
# transcribe_audio — missing credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_when_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASR_PROVIDER",
        "azure_openai",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_API_KEY",
        "",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_DEPLOYMENT",
        "gpt-4o-transcribe",
    )

    assert await transcribe_audio(b"audio") == ""


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_when_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASR_PROVIDER",
        "azure_openai",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_API_KEY",
        "key",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_ENDPOINT",
        "",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_DEPLOYMENT",
        "gpt-4o-transcribe",
    )

    assert await transcribe_audio(b"audio") == ""


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_when_missing_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASR_PROVIDER",
        "azure_openai",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_API_KEY",
        "key",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_DEPLOYMENT",
        "",
    )

    assert await transcribe_audio(b"audio") == ""


# ---------------------------------------------------------------------------
# transcribe_audio — success path
# ---------------------------------------------------------------------------


def _patch_transcription_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASR_PROVIDER",
        "azure_openai",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_API_KEY",
        "test-key",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_DEPLOYMENT",
        "gpt-4o-transcribe",
    )
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.AZURE_TRANSCRIPTION_API_VERSION",
        "2025-03-01-preview",
    )


@pytest.mark.asyncio
async def test_transcribe_audio_uses_azure_rest_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transcription_config(monkeypatch)

    captured: dict[str, object] = {}

    class _FakeResponse:
        headers = {"content-type": "application/json"}

        @staticmethod
        def json() -> dict[str, str]:
            return {"text": "create the task"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, data, files):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            captured["file_tuple"] = files["file"]
            return _FakeResponse()

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    transcript = await transcribe_audio(
        b"audio-bytes",
        filename="voice-note.m4a",
        language="en",
    )

    assert transcript == "create the task"
    assert captured["timeout"] == 60.0
    assert captured["url"] == (
        "https://example.openai.azure.com/openai/deployments/gpt-4o-transcribe/"
        "audio/transcriptions?api-version=2025-03-01-preview"
    )
    assert captured["headers"] == {"api-key": "test-key"}
    assert captured["data"] == {"model": "gpt-4o-transcribe", "language": "en"}
    filename, file_obj, mime_type = captured["file_tuple"]
    assert filename == "voice-note.m4a"
    assert file_obj.read() == b"audio-bytes"
    assert mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_transcribe_audio_omits_language_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transcription_config(monkeypatch)

    captured: dict[str, object] = {}

    class _FakeResponse:
        headers = {"content-type": "application/json"}

        @staticmethod
        def json() -> dict[str, str]:
            return {"text": "transcript"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, data, files):
            captured["data"] = data
            return _FakeResponse()

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    await transcribe_audio(b"audio-bytes")

    assert "language" not in captured["data"]


# ---------------------------------------------------------------------------
# transcribe_audio — AssemblyAI provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_audio_uses_assemblyai_upload_submit_and_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_assemblyai_config(monkeypatch)

    captured: list[dict[str, object]] = []

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def json(self) -> dict[str, object]:
            return self._payload

        @staticmethod
        def raise_for_status() -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            captured.append({"method": "client", "timeout": timeout})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, content=None, json=None):
            captured.append(
                {
                    "method": "post",
                    "url": url,
                    "headers": headers,
                    "content": content,
                    "json": json,
                }
            )
            if url.endswith("/v2/upload"):
                return _FakeResponse({"upload_url": "https://cdn.assemblyai.com/upload/audio"})
            return _FakeResponse({"id": "transcript-123", "status": "queued"})

        async def get(self, url, *, headers):
            captured.append({"method": "get", "url": url, "headers": headers})
            return _FakeResponse({"status": "completed", "text": "create the task"})

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    transcript = await transcribe_audio(
        b"audio-bytes",
        filename="voice-note.m4a",
        language="en",
    )

    assert transcript == "create the task"
    assert captured[0] == {"method": "client", "timeout": 60.0}
    assert captured[1] == {
        "method": "post",
        "url": "https://api.assemblyai.com/v2/upload",
        "headers": {
            "authorization": "assembly-key",
            "content-type": "application/octet-stream",
        },
        "content": b"audio-bytes",
        "json": None,
    }
    assert captured[2] == {
        "method": "post",
        "url": "https://api.assemblyai.com/v2/transcript",
        "headers": {"authorization": "assembly-key"},
        "content": None,
        "json": {
            "audio_url": "https://cdn.assemblyai.com/upload/audio",
            "language_detection": True,
            "speech_models": ["universal-3-5-pro"],
            "language_code": "en",
        },
    }
    assert captured[3] == {
        "method": "get",
        "url": "https://api.assemblyai.com/v2/transcript/transcript-123",
        "headers": {"authorization": "assembly-key"},
    }


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_when_assemblyai_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_assemblyai_config(monkeypatch)
    monkeypatch.setattr(
        "src.infra.media.audio_transcription.transcription_config.ASSEMBLYAI_API_KEY",
        "",
    )

    assert await transcribe_audio(b"audio") == ""


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_when_assemblyai_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_assemblyai_config(monkeypatch)

    class _FakeResponse:
        headers = {"content-type": "application/json"}

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "error", "error": "bad audio"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, content=None, json=None):
            if url.endswith("/v2/upload"):
                return _FakeResponse()
            return type(
                "_SubmitResponse",
                (),
                {
                    "headers": {"content-type": "application/json"},
                    "json": staticmethod(lambda: {"id": "transcript-123"}),
                    "raise_for_status": staticmethod(lambda: None),
                },
            )()

        async def get(self, url, *, headers):
            return _FakeResponse()

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    assert await transcribe_audio(b"audio-bytes", filename="test.webm") == ""


# ---------------------------------------------------------------------------
# transcribe_audio — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transcription_config(monkeypatch)

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, data, files):
            raise httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("POST", url),
                response=httpx.Response(500),
            )

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    result = await transcribe_audio(b"audio-bytes", filename="test.webm")
    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_audio_returns_empty_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transcription_config(monkeypatch)

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, data, files):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr("src.infra.media.audio_transcription.httpx.AsyncClient", _FakeAsyncClient)

    result = await transcribe_audio(b"audio-bytes", filename="test.webm")
    assert result == ""
