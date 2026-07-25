"""Contracts for listing and reading one provider-backed email attachment."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.tools.communication.email_attachments import (
    CachedEmailAttachment,
    ReadableEmailSource,
    _extract_text_chunk,
    create_read_email_attachment,
    list_email_attachments,
    read_email_attachment,
)
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.infra.config.configs import chat_file_upload_config
from src.workers.integrations_worker.providers.email.attachment_downloader import (
    AttachmentMeta,
    DownloadedAttachment,
)


def _source() -> ReadableEmailSource:
    return ReadableEmailSource(
        email_id=uuid4(),
        external_message_id="provider-message-1",
        internet_message_id="<message@example.com>",
        integration_type=IntegrationType.GOOGLE,
        token_info=SimpleNamespace(),
    )


def test_extract_text_chunk_returns_a_non_overlapping_continuation():
    """A long document exposes an explicit cursor instead of silently ending."""
    content = b"abcdefghij"

    first = _extract_text_chunk(
        content,
        mimetype="text/plain",
        filename="notes.txt",
        offset=0,
        max_chars=4,
    )
    second = _extract_text_chunk(
        content,
        mimetype="text/plain",
        filename="notes.txt",
        offset=first["next_offset"],
        max_chars=4,
    )

    assert first == {
        "content": "abcd",
        "offset": 0,
        "returned_characters": 4,
        "truncated": True,
        "next_offset": 4,
    }
    assert second["content"] == "efgh"
    assert second["offset"] == 4
    assert second["next_offset"] == 8


@pytest.mark.asyncio
async def test_email_attachment_activity_uses_filename_without_trusting_it(monkeypatch):
    """The progress label may use the name, but execution must still use the IDs."""
    calls = []

    async def _execute(_capabilities, **kwargs):
        calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.execute_with_capabilities",
        _execute,
    )
    tool = create_read_email_attachment(SimpleNamespace(), lambda: SimpleNamespace())

    await tool(
        "<message@example.com>",
        "opaque-provider-id",
        filename="Exotel-Mutual-NDA-Final.docx",
        mailbox_email="seller@example.com",
    )
    await tool(
        "<message@example.com>",
        "opaque-provider-id",
        mailbox_email="seller@example.com",
    )

    assert [call["description"] for call in calls] == [
        "Reading email attachment: Exotel-Mutual-NDA-Final.docx",
        "Reading email attachment",
    ]
    assert all("opaque-provider-id" not in call["description"] for call in calls)
    assert all("filename" not in call["parameters"] for call in calls)


@pytest.mark.asyncio
async def test_list_email_attachments_preserves_message_ownership(monkeypatch):
    """Every provider handle remains paired with the RFC message that owns it."""
    source = _source()

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [
            AttachmentMeta(
                filename="contract.pdf",
                mime_type="application/pdf",
                size_bytes=42_000,
                attachment_id="att-1",
            ),
            AttachmentMeta(
                filename="pricing.xlsx",
                mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                size_bytes=21_000,
                attachment_id="att-2",
            ),
        ]

    async def _persist(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._persist_attachment_metadata",
        _persist,
    )

    result = await list_email_attachments(
        source.internet_message_id,
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
    )

    assert result["status"] == "success"
    assert [
        (item["internet_message_id"], item["attachment_id"]) for item in result["attachments"]
    ] == [
        ("<message@example.com>", "att-1"),
        ("<message@example.com>", "att-2"),
    ]


@pytest.mark.asyncio
async def test_read_email_attachment_reads_only_the_selected_attachment(monkeypatch):
    """One tool call downloads one validated attachment, never its siblings."""
    source = _source()
    downloaded_ids: list[str] = []

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [
            AttachmentMeta("first.txt", "text/plain", 1_024, "att-1"),
            AttachmentMeta("second.txt", "text/plain", 1_024, "att-2"),
        ]

    async def _download(_source, attachment):
        downloaded_ids.append(attachment.attachment_id)
        return DownloadedAttachment(content=b"0123456789", size_bytes=10)

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-2",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
        max_chars=4,
    )

    assert downloaded_ids == ["att-2"]
    assert result["attachment_id"] == "att-2"
    assert result["content"] == "0123"
    assert result["truncated"] is True
    assert result["next_offset"] == 4


@pytest.mark.asyncio
async def test_read_email_attachment_caps_requested_chunk_size(monkeypatch):
    """A tool argument cannot expand one result beyond the configured context budget."""
    source = _source()
    limit = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS
    content = b"x" * (limit + 1)

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [AttachmentMeta("notes.txt", "text/plain", len(content), "att-1")]

    async def _download(_source, _attachment):
        return DownloadedAttachment(content=content, size_bytes=len(content))

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-1",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
        max_chars=limit * 2,
    )

    assert result["returned_characters"] == limit
    assert result["truncated"] is True
    assert result["next_offset"] == limit


@pytest.mark.asyncio
async def test_read_email_attachment_reuses_existing_deal_asset_blob(monkeypatch):
    """Deal reads prefer the durable Azure copy over another provider download."""
    source = _source()
    deal_id = str(uuid4())

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [AttachmentMeta("contract.txt", "text/plain", 1_024, "att-1")]

    async def _cached(*_args, **_kwargs):
        return CachedEmailAttachment(
            attachment=AttachmentMeta(
                "contract.txt",
                "text/plain",
                1_024,
                "att-1",
            ),
            downloaded=DownloadedAttachment(content=b"cached contract", size_bytes=15),
        )

    async def _unexpected_provider_download(*_args):
        pytest.fail("an existing deal asset must not be downloaded again")

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._read_cached_deal_attachment",
        _cached,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _unexpected_provider_download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-1",
        context=SimpleNamespace(),
        deal_id=deal_id,
    )

    assert result["content"] == "cached contract"


@pytest.mark.asyncio
async def test_read_email_attachment_rejects_handle_from_another_message(monkeypatch):
    """A guessed provider attachment ID cannot bypass message ownership."""
    source = _source()

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [AttachmentMeta("real.pdf", "application/pdf", 100, "att-real")]

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-from-another-message",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
    )

    assert result["status"] == "error"
    assert result["code"] == "attachment_not_found"


@pytest.mark.asyncio
async def test_read_email_attachment_rejects_unsupported_type_before_download(monkeypatch):
    """Unsupported archives are named explicitly and never sent to a parser."""
    source = _source()

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [AttachmentMeta("archive.zip", "application/zip", 100, "att-zip")]

    async def _unexpected_download(*_args):
        pytest.fail("unsupported attachment must not be downloaded")

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _unexpected_download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-zip",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
    )

    assert result["status"] == "error"
    assert result["code"] == "unsupported_attachment_type"


@pytest.mark.asyncio
async def test_read_email_attachment_reports_provider_oversize_before_download(monkeypatch):
    """A known oversized file is reported accurately instead of as inline noise."""
    source = _source()

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [
            AttachmentMeta(
                "huge.pdf",
                "application/pdf",
                25 * 1024 * 1024 + 1,
                "att-huge",
            )
        ]

    async def _unexpected_download(*_args):
        pytest.fail("known oversized attachment must not be downloaded")

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _unexpected_download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-huge",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
    )

    assert result["code"] == "attachment_too_large"


@pytest.mark.asyncio
async def test_read_email_attachment_does_not_report_bad_encoding_as_oversized(
    monkeypatch,
):
    """Malformed provider data is a download failure, not a misleading size error."""
    source = _source()

    async def _resolve(**_kwargs):
        return source

    async def _list(_source):
        return [AttachmentMeta("document.pdf", "application/pdf", 2_000, "att-1")]

    async def _download(*_args):
        raise ValueError("Invalid Gmail attachment encoding")

    async def _no_cache(**_kwargs):
        return None

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.resolve_readable_email_source",
        _resolve,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._list_provider_attachments",
        _list,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._read_cached_deal_attachment",
        _no_cache,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments._download_provider_attachment",
        _download,
    )

    result = await read_email_attachment(
        source.internet_message_id,
        "att-1",
        context=SimpleNamespace(),
        mailbox_email="seller@example.com",
    )

    assert result["code"] == "attachment_download_failed"


@pytest.mark.asyncio
async def test_read_email_attachment_requires_exactly_one_access_scope():
    """Mailbox and deal scopes cannot be combined or omitted."""
    context = SimpleNamespace()

    missing = await read_email_attachment(
        "<message@example.com>",
        "att-1",
        context=context,
    )
    conflicting = await read_email_attachment(
        "<message@example.com>",
        "att-1",
        context=context,
        mailbox_email="seller@example.com",
        deal_id=str(uuid4()),
    )

    assert missing["code"] == "email_scope_required"
    assert conflicting["code"] == "email_scope_conflict"
