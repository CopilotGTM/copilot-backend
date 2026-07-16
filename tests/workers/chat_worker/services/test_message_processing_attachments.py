from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.chat_worker.services.message_processing_service import (
    AttachmentInfo,
    MessageProcessingService,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, organisation_id):
        self.organisation_id = organisation_id
        self.paths_read: list[str] = []

    async def get_blob_content(self, storage_path: str) -> bytes:
        self.paths_read.append(storage_path)
        return b"image-bytes"

    async def close(self) -> None:
        return None


class _FakeImage:
    def __init__(self, content: bytes, format: str = "png"):  # noqa: A002
        self.content = content
        self.format = format


def _patch_storage(monkeypatch, storage_cls=_FakeStorage):
    monkeypatch.setattr(
        "src.infra.storage.azure_org_blob_storage.AzureOrganisationBlobStorage",
        storage_cls,
    )


def _patch_image(monkeypatch):
    monkeypatch.setattr("agno.media.Image", _FakeImage)


# ---------------------------------------------------------------------------
# _resolve_attachments — non-image filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_skips_blob_reads_for_non_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())

    class _FailIfConstructed:
        def __init__(self, *args, **kwargs):
            raise AssertionError("blob storage should not be used for non-image attachments")

    monkeypatch.setattr(
        "src.infra.storage.azure_org_blob_storage.AzureOrganisationBlobStorage",
        _FailIfConstructed,
    )

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/doc.txt",
                mimetype="text/plain",
                filename="doc.txt",
            )
        ]
    )

    assert images == []


# ---------------------------------------------------------------------------
# _resolve_attachments — image loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_loads_current_turn_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())
    captured_paths: list[str] = []

    class _TrackingStorage(_FakeStorage):
        async def get_blob_content(self, storage_path: str) -> bytes:
            captured_paths.append(storage_path)
            return b"image-bytes"

    _patch_storage(monkeypatch, _TrackingStorage)
    _patch_image(monkeypatch)

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/image.png",
                mimetype="image/png",
                filename="image.png",
            ),
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/doc.txt",
                mimetype="text/plain",
                filename="doc.txt",
            ),
        ]
    )

    assert captured_paths == ["attachments/image.png"]
    assert len(images) == 1
    assert images[0].content == b"image-bytes"
    assert images[0].format == "png"


# ---------------------------------------------------------------------------
# _resolve_attachments — multiple images
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_loads_multiple_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())
    _patch_storage(monkeypatch)
    _patch_image(monkeypatch)

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/img1.png",
                mimetype="image/png",
                filename="img1.png",
            ),
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/img2.jpeg",
                mimetype="image/jpeg",
                filename="img2.jpeg",
            ),
        ]
    )

    assert len(images) == 2
    assert images[0].format == "png"
    assert images[1].format == "jpeg"


# ---------------------------------------------------------------------------
# _resolve_attachments — empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_empty_list() -> None:
    service = MessageProcessingService(uuid4())
    images = await service._resolve_attachments([])
    assert images == []


# ---------------------------------------------------------------------------
# _resolve_attachments — mimetype-to-format mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mimetype,expected_format",
    [
        ("image/png", "png"),
        ("image/jpeg", "jpeg"),
        ("image/gif", "gif"),
        ("image/webp", "webp"),
    ],
)
async def test_resolve_attachments_format_from_mimetype(
    monkeypatch: pytest.MonkeyPatch,
    mimetype: str,
    expected_format: str,
) -> None:
    service = MessageProcessingService(uuid4())
    _patch_storage(monkeypatch)
    _patch_image(monkeypatch)

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path=f"attachments/img.{expected_format}",
                mimetype=mimetype,
                filename=f"img.{expected_format}",
            ),
        ]
    )

    assert len(images) == 1
    assert images[0].format == expected_format


# ---------------------------------------------------------------------------
# _resolve_attachments — storage failure for one image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_skips_failed_blob_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())
    call_count = 0

    class _FailFirstStorage:
        def __init__(self, organisation_id):
            pass

        async def get_blob_content(self, storage_path: str) -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("blob not found")
            return b"second-image"

        async def close(self) -> None:
            return None

    _patch_storage(monkeypatch, _FailFirstStorage)
    _patch_image(monkeypatch)

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/missing.png",
                mimetype="image/png",
                filename="missing.png",
            ),
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="attachments/ok.png",
                mimetype="image/png",
                filename="ok.png",
            ),
        ]
    )

    assert len(images) == 1
    assert images[0].content == b"second-image"


# ---------------------------------------------------------------------------
# _resolve_attachments — skips image with no storage_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_attachments_skips_no_storage_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())
    _patch_storage(monkeypatch)
    _patch_image(monkeypatch)

    images = await service._resolve_attachments(
        [
            AttachmentInfo(
                attachment_id=uuid4(),
                storage_path="",
                mimetype="image/png",
                filename="no-path.png",
            ),
        ]
    )

    assert images == []


@pytest.mark.asyncio
async def test_webchat_defers_current_images_to_shared_agent_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MessageProcessingService(uuid4())
    captured: dict[str, object] = {}

    async def fake_master_session():
        yield object()

    async def fake_get_organisation_name(_session, _organisation_id):
        return "Test Org"

    async def fake_run_agent_for_chat(**kwargs):
        captured.update(kwargs)
        if False:
            yield None

    async def fail_duplicate_loader(*_args, **_kwargs):
        raise AssertionError("WebChat must not load images before GeneralAgent")

    monkeypatch.setattr(
        "src.workers.chat_worker.services.message_processing_service.acquire_master_async_session",
        fake_master_session,
    )
    monkeypatch.setattr(
        "src.workers.chat_worker.services.message_processing_service.get_organisation_name",
        fake_get_organisation_name,
    )
    monkeypatch.setattr("src.agents.orchestrator.run_agent_for_chat", fake_run_agent_for_chat)
    monkeypatch.setattr(service, "_resolve_attachments", fail_duplicate_loader)

    attachment = AttachmentInfo(
        attachment_id=uuid4(),
        storage_path="attachments/image.png",
        mimetype="image/png",
        filename="image.png",
    )
    history = [
        {
            "role": "user",
            "content": "inspect this",
            "attachment_manifest": (
                f"[Attached files]\n- id: {attachment.attachment_id}\n"
                "  name: image.png\n  type: image/png"
            ),
        }
    ]
    data = SimpleNamespace(
        user_id=uuid4(),
        user_email="user@example.com",
        assistant_message_id=uuid4(),
        session_id=uuid4(),
        user_message_id=uuid4(),
        attachments=[attachment],
        job_metadata={},
        chat_type="GENERAL_CHAT",
        history=history,
        job_id=uuid4(),
    )

    await service._consume_agent_stream(data)

    assert captured["images"] is None
    assert captured["messages"] == history
