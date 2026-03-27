from __future__ import annotations

from src.service.chat.chat_attachment_manifest import (
    AttachmentContextItem,
    append_attachment_manifest,
    build_attachment_manifest,
    serialize_attachment_context,
)

# ---------------------------------------------------------------------------
# build_attachment_manifest
# ---------------------------------------------------------------------------


def test_build_manifest_from_dataclass_items() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="abc-123",
            filename="report.pdf",
            mimetype="application/pdf",
            source_provider="web",
        ),
    ]
    result = build_attachment_manifest(items)

    assert "[Attached files]" in result
    assert "- id: abc-123" in result
    assert "  name: report.pdf" in result
    assert "  type: application/pdf" in result
    assert "  source: web" in result


def test_build_manifest_from_dict_items() -> None:
    items = [
        {
            "attachment_id": "abc-123",
            "filename": "report.pdf",
            "mimetype": "application/pdf",
        }
    ]
    result = build_attachment_manifest(items)

    assert "- id: abc-123" in result
    assert "  name: report.pdf" in result
    assert "  source:" not in result


def test_build_manifest_empty_attachments() -> None:
    assert build_attachment_manifest([]) == ""


def test_build_manifest_respects_max_items() -> None:
    items = [
        AttachmentContextItem(
            attachment_id=f"id-{i}",
            filename=f"file-{i}.txt",
            mimetype="text/plain",
        )
        for i in range(15)
    ]
    result = build_attachment_manifest(items, max_items=3)

    assert result.count("- id:") == 3
    assert "... and 12 more attachment(s)" in result


def test_build_manifest_no_overflow_when_within_limit() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="file.txt",
            mimetype="text/plain",
        )
    ]
    result = build_attachment_manifest(items, max_items=5)

    assert "more attachment(s)" not in result


def test_build_manifest_dict_defaults_for_missing_fields() -> None:
    items = [{"some_other_key": "value"}]
    result = build_attachment_manifest(items)

    assert "  name: attachment" in result
    assert "  type: application/octet-stream" in result


def test_build_manifest_source_provider_none_omits_source_line() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="file.txt",
            mimetype="text/plain",
            source_provider=None,
        )
    ]
    result = build_attachment_manifest(items)
    assert "source:" not in result


# ---------------------------------------------------------------------------
# append_attachment_manifest
# ---------------------------------------------------------------------------


def test_append_manifest_to_content() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="doc.pdf",
            mimetype="application/pdf",
        )
    ]
    result = append_attachment_manifest("Hello", items)

    assert result.startswith("Hello")
    assert "[Attached files]" in result
    assert "\n\n" in result


def test_append_manifest_to_empty_content() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="doc.pdf",
            mimetype="application/pdf",
        )
    ]
    result = append_attachment_manifest("", items)

    assert result.startswith("[Attached files]")


def test_append_manifest_to_none_content() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="doc.pdf",
            mimetype="application/pdf",
        )
    ]
    result = append_attachment_manifest(None, items)

    assert result.startswith("[Attached files]")


def test_append_manifest_with_no_attachments() -> None:
    assert append_attachment_manifest("Hello", []) == "Hello"


def test_append_manifest_with_none_content_and_no_attachments() -> None:
    assert append_attachment_manifest(None, []) == ""


# ---------------------------------------------------------------------------
# serialize_attachment_context
# ---------------------------------------------------------------------------


def test_serialize_attachment_context_from_dataclass() -> None:
    items = [
        AttachmentContextItem(
            attachment_id="id-1",
            filename="file.txt",
            mimetype="text/plain",
            source_provider="slack",
        ),
    ]
    result = serialize_attachment_context(items)

    assert result == [
        {
            "attachment_id": "id-1",
            "filename": "file.txt",
            "mimetype": "text/plain",
            "source_provider": "slack",
        }
    ]


def test_serialize_attachment_context_from_dict() -> None:
    items = [{"attachment_id": "id-1", "filename": "file.txt", "mimetype": "text/plain"}]
    result = serialize_attachment_context(items)

    assert len(result) == 1
    assert result[0]["attachment_id"] == "id-1"
    assert result[0]["source_provider"] is None


def test_serialize_attachment_context_empty() -> None:
    assert serialize_attachment_context([]) == []
