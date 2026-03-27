from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agno.tools.function import ToolResult as AgnoToolResult
from src.agents.tools.communication.chat_attachments import (
    _truncate_text,
    list_chat_attachments,
    read_chat_attachment,
)

# ---------------------------------------------------------------------------
# Fakes shared across tests
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _FakeSession:
    def __init__(self, execute_results, message=None):
        self._execute_results = list(execute_results)
        self._message = message

    async def execute(self, _stmt):
        return _FakeResult(self._execute_results.pop(0))

    async def get(self, _model, _message_id):
        return self._message


class _FakeContext:
    def __init__(self, organisation_id, user_id, metadata, session):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.metadata = metadata
        self._session = session

    @asynccontextmanager
    async def acquire_session(self):
        yield self._session


class _FakeStorage:
    def __init__(self, _organisation_id):
        self.closed = False
        self._content = b"fake-content"

    async def get_blob_content(self, _storage_path):
        return self._content

    async def close(self):
        self.closed = True


def _patch_storage(monkeypatch, content: bytes = b"fake-content"):
    class Storage(_FakeStorage):
        def __init__(self, _organisation_id):
            super().__init__(_organisation_id)
            self._content = content

    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.AzureOrganisationBlobStorage",
        Storage,
    )


def _make_attachment(
    attachment_id=None,
    filename="file.txt",
    mimetype="text/plain",
    storage_path="attachments/file.txt",
    organisation_id=None,
    source_provider=None,
):
    return SimpleNamespace(
        attachment_id=attachment_id or uuid4(),
        filename=filename,
        mimetype=mimetype,
        storage_path=storage_path,
        organisation_id=organisation_id or uuid4(),
        is_deleted=False,
        status="completed",
        source_provider=source_provider,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# _truncate_text
# ---------------------------------------------------------------------------


def test_truncate_text_within_limit() -> None:
    content, truncated = _truncate_text("hello", 100)
    assert content == "hello"
    assert truncated is False


def test_truncate_text_at_exact_limit() -> None:
    content, truncated = _truncate_text("ab", 2)
    assert content == "ab"
    assert truncated is False


def test_truncate_text_over_limit() -> None:
    content, truncated = _truncate_text("abcdef", 3)
    assert content == "abc"
    assert truncated is True


# ---------------------------------------------------------------------------
# list_chat_attachments — bot attachment contexts (legacy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chat_attachments_includes_bot_attachment_contexts() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    created_at = datetime.now(UTC)
    message = SimpleNamespace(
        message_id=message_id,
        session_id=session_id,
        created_at=created_at,
        extra={
            "attachment_contexts": [
                {
                    "filename": "notes.txt",
                    "mimetype": "text/plain",
                    "content": "hello world",
                }
            ]
        },
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[message], []]),
    )

    result = await list_chat_attachments(context=context)

    assert result["scope"] == "conversation"
    assert result["attachments"] == [
        {
            "attachment_id": f"ctx:{message_id}:0",
            "message_id": str(message_id),
            "filename": "notes.txt",
            "mimetype": "text/plain",
            "source_provider": None,
            "created_at": created_at.isoformat(),
        }
    ]


# ---------------------------------------------------------------------------
# list_chat_attachments — scope normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chat_attachments_normalizes_invalid_scope() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    message = SimpleNamespace(
        message_id=uuid4(),
        session_id=session_id,
        created_at=datetime.now(UTC),
        extra={},
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[message], []]),
    )

    result = await list_chat_attachments(context=context, scope="invalid_scope")

    assert result["scope"] == "conversation"


# ---------------------------------------------------------------------------
# list_chat_attachments — no session_id and no allowed_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chat_attachments_no_session_no_allowed_ids() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        metadata={},
        session=_FakeSession([]),
    )

    result = await list_chat_attachments(context=context)

    assert result["scope"] == "conversation"
    assert result["attachments"] == []


# ---------------------------------------------------------------------------
# read_chat_attachment — unavailable for binary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_returns_unavailable_for_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="archive.bin",
        mimetype="application/octet-stream",
        storage_path="attachments/archive.bin",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"\x00\x01\x02")
    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.extract_text_from_bytes",
        lambda *_args, **_kwargs: "",
    )

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert result == {
        "attachment_id": str(attachment_id),
        "filename": "archive.bin",
        "mimetype": "application/octet-stream",
        "content_type": "unavailable",
        "content": "No readable text could be extracted from this attachment.",
        "truncated": False,
    }


# ---------------------------------------------------------------------------
# read_chat_attachment — image returns AgnoToolResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_returns_image_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="overview.png",
        mimetype="image/png",
        storage_path="attachments/overview.png",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"fake-image-bytes")

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert isinstance(result, AgnoToolResult)
    assert (
        result.content
        == "Opened image attachment overview.png. Analyze the image directly before answering."
    )
    assert result.images is not None
    assert len(result.images) == 1
    assert result.images[0].content == b"fake-image-bytes"
    assert result.images[0].mime_type == "image/png"


# ---------------------------------------------------------------------------
# read_chat_attachment — text extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_extracts_text_from_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="report.pdf",
        mimetype="application/pdf",
        storage_path="attachments/report.pdf",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"pdf-bytes")
    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.extract_text_from_bytes",
        lambda content, mimetype, filename: "Extracted PDF content here",
    )

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert result["content_type"] == "text"
    assert result["content"] == "Extracted PDF content here"
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# read_chat_attachment — audio transcription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_transcribes_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="voice.webm",
        mimetype="audio/webm",
        storage_path="attachments/voice.webm",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"audio-bytes")
    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.transcribe_audio",
        lambda *_args, **_kwargs: "transcribed audio text",
    )
    # Make transcribe_audio an async function

    async def fake_transcribe(content, filename):
        return "transcribed audio text"

    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.transcribe_audio",
        fake_transcribe,
    )

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert result["content_type"] == "audio_transcript"
    assert result["content"] == "transcribed audio text"
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# read_chat_attachment — truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_truncates_long_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="big.txt",
        mimetype="text/plain",
        storage_path="attachments/big.txt",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"x" * 50000)
    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.extract_text_from_bytes",
        lambda content, mimetype, filename: "x" * 50000,
    )

    result = await read_chat_attachment(str(attachment_id), context=context, max_chars=100)

    assert result["truncated"] is True
    assert len(result["content"]) == 100


# ---------------------------------------------------------------------------
# read_chat_attachment — no session_id and no allowed_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_no_context_returns_not_found() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        metadata={},
        session=_FakeSession([]),
    )

    result = await read_chat_attachment(str(uuid4()), context=context)

    assert result is not None
    assert "not_found" in str(result.get("status", "")) or "No chat attachment" in str(result)


# ---------------------------------------------------------------------------
# read_chat_attachment — invalid attachment_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_invalid_uuid_returns_error() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        metadata={"chat_session_id": str(uuid4())},
        session=_FakeSession([]),
    )

    result = await read_chat_attachment("not-a-uuid", context=context)

    assert result is not None
    assert "valid attachment UUID" in str(result)


# ---------------------------------------------------------------------------
# read_chat_attachment — missing storage_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_missing_storage_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="file.txt",
        mimetype="text/plain",
        organisation_id=organisation_id,
    )
    attachment.storage_path = None
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[attachment]]),
    )

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert "storage path unavailable" in str(result).lower()


# ---------------------------------------------------------------------------
# read_chat_attachment — allowed_attachment_ids security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_via_allowed_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    attachment_id = uuid4()
    attachment = _make_attachment(
        attachment_id=attachment_id,
        filename="doc.txt",
        mimetype="text/plain",
        storage_path="attachments/doc.txt",
        organisation_id=organisation_id,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"allowed_attachment_ids": [str(attachment_id)]},
        session=_FakeSession([[attachment]]),
    )

    _patch_storage(monkeypatch, b"allowed content")
    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.extract_text_from_bytes",
        lambda content, mimetype, filename: "allowed content",
    )

    result = await read_chat_attachment(str(attachment_id), context=context)

    assert result["content"] == "allowed content"


# ---------------------------------------------------------------------------
# read_chat_attachment — legacy bot context (ctx: prefix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_legacy_bot_context_attachment() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()

    message = SimpleNamespace(
        message_id=message_id,
        session_id=session_id,
        extra={
            "attachment_contexts": [
                {
                    "filename": "notes.txt",
                    "mimetype": "text/plain",
                    "content": "hello world",
                    "content_type": "text",
                }
            ]
        },
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([], message=message),
    )

    result = await read_chat_attachment(f"ctx:{message_id}:0", context=context)

    assert result["content"] == "hello world"
    assert result["filename"] == "notes.txt"
    assert result["content_type"] == "text"


@pytest.mark.asyncio
async def test_read_legacy_bot_context_invalid_index() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()

    message = SimpleNamespace(
        message_id=message_id,
        session_id=session_id,
        extra={"attachment_contexts": []},
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([], message=message),
    )

    result = await read_chat_attachment(f"ctx:{message_id}:0", context=context)

    assert "not found" in str(result).lower() or "not_found" in str(result)


@pytest.mark.asyncio
async def test_read_legacy_bot_context_no_session_id() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        metadata={},
        session=_FakeSession([]),
    )

    result = await read_chat_attachment("ctx:some-id:0", context=context)

    assert result is not None


@pytest.mark.asyncio
async def test_read_legacy_bot_context_truncates_content() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()

    long_content = "x" * 50000
    message = SimpleNamespace(
        message_id=message_id,
        session_id=session_id,
        extra={
            "attachment_contexts": [
                {
                    "filename": "big.txt",
                    "mimetype": "text/plain",
                    "content": long_content,
                    "content_type": "text",
                }
            ]
        },
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([], message=message),
    )

    result = await read_chat_attachment(f"ctx:{message_id}:0", context=context, max_chars=100)

    assert result["truncated"] is True
    assert len(result["content"]) == 100


# ---------------------------------------------------------------------------
# read_chat_attachment — attachment not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chat_attachment_not_found_in_session() -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=uuid4(),
        metadata={"chat_session_id": str(session_id)},
        session=_FakeSession([[None]]),
    )

    result = await read_chat_attachment(str(uuid4()), context=context)

    assert "not found" in str(result).lower() or "not_found" in str(result)
