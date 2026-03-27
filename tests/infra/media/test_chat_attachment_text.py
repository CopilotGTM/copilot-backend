from __future__ import annotations

import pytest
from src.infra.media.chat_attachment_text import (
    AUDIO_MIMETYPES,
    IMAGE_MIMETYPES,
    extract_text_from_bytes,
    is_audio_mimetype,
    is_image_mimetype,
)

# ---------------------------------------------------------------------------
# is_image_mimetype
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mimetype", list(IMAGE_MIMETYPES))
def test_is_image_mimetype_accepts_all_defined_types(mimetype: str) -> None:
    assert is_image_mimetype(mimetype) is True


def test_is_image_mimetype_is_case_insensitive() -> None:
    assert is_image_mimetype("IMAGE/PNG") is True
    assert is_image_mimetype("Image/Jpeg") is True


def test_is_image_mimetype_rejects_non_image() -> None:
    assert is_image_mimetype("application/pdf") is False
    assert is_image_mimetype("audio/mpeg") is False
    assert is_image_mimetype("text/plain") is False


def test_is_image_mimetype_handles_empty_and_none() -> None:
    assert is_image_mimetype("") is False
    assert is_image_mimetype(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_audio_mimetype
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mimetype", list(AUDIO_MIMETYPES))
def test_is_audio_mimetype_accepts_all_defined_types(mimetype: str) -> None:
    assert is_audio_mimetype(mimetype) is True


def test_is_audio_mimetype_is_case_insensitive() -> None:
    assert is_audio_mimetype("AUDIO/MPEG") is True
    assert is_audio_mimetype("Audio/Wav") is True


def test_is_audio_mimetype_rejects_non_audio() -> None:
    assert is_audio_mimetype("image/png") is False
    assert is_audio_mimetype("text/plain") is False


def test_is_audio_mimetype_handles_empty_and_none() -> None:
    assert is_audio_mimetype("") is False
    assert is_audio_mimetype(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_text_from_bytes — plain text and text-like formats
# ---------------------------------------------------------------------------


def test_extract_text_from_plain_text() -> None:
    content = b"Hello, world!"
    result = extract_text_from_bytes(content, "text/plain", "hello.txt")
    assert result == "Hello, world!"


def test_extract_text_from_csv() -> None:
    content = b"name,age\nAlice,30\nBob,25"
    result = extract_text_from_bytes(content, "text/csv", "data.csv")
    assert "Alice,30" in result


def test_extract_text_from_json() -> None:
    content = b'{"key": "value"}'
    result = extract_text_from_bytes(content, "application/json", "data.json")
    assert '"key"' in result


def test_extract_text_from_xml() -> None:
    content = b"<root><item>text</item></root>"
    result = extract_text_from_bytes(content, "application/xml", "data.xml")
    assert "<item>text</item>" in result


def test_extract_text_from_ndjson() -> None:
    content = b'{"a":1}\n{"b":2}'
    result = extract_text_from_bytes(content, "application/x-ndjson", "data.ndjson")
    assert '{"a":1}' in result


def test_extract_text_from_jsonl() -> None:
    content = b'{"a":1}\n{"b":2}'
    result = extract_text_from_bytes(content, "application/jsonl", "data.jsonl")
    assert '{"a":1}' in result


def test_extract_text_from_markdown() -> None:
    content = b"# Heading\n\nParagraph text"
    result = extract_text_from_bytes(content, "text/markdown", "notes.md")
    assert "# Heading" in result


def test_extract_text_handles_utf8_with_replacement() -> None:
    content = b"Hello \xff world"
    result = extract_text_from_bytes(content, "text/plain", "test.txt")
    assert "Hello" in result
    assert "world" in result


# ---------------------------------------------------------------------------
# extract_text_from_bytes — unknown / binary formats
# ---------------------------------------------------------------------------


def test_extract_text_returns_empty_for_unknown_mimetype() -> None:
    assert extract_text_from_bytes(b"\x00\x01", "application/octet-stream", "file.bin") == ""


def test_extract_text_returns_empty_for_image_mimetype() -> None:
    assert extract_text_from_bytes(b"\x89PNG", "image/png", "pic.png") == ""


def test_extract_text_returns_empty_for_audio_mimetype() -> None:
    assert extract_text_from_bytes(b"\xff\xfb", "audio/mpeg", "song.mp3") == ""


def test_extract_text_handles_none_mimetype() -> None:
    assert extract_text_from_bytes(b"data", None, "file") == ""  # type: ignore[arg-type]


def test_extract_text_handles_empty_mimetype() -> None:
    assert extract_text_from_bytes(b"data", "", "file") == ""


# ---------------------------------------------------------------------------
# extract_text_from_bytes — PDF
# ---------------------------------------------------------------------------


def test_extract_text_from_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePage:
        def extract_text(self):
            return "Page content"

    class _FakeReader:
        def __init__(self, _stream):
            self.pages = [_FakePage(), _FakePage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)

    result = extract_text_from_bytes(b"%PDF-fake", "application/pdf", "doc.pdf")
    assert result == "Page content\n\nPage content"


def test_extract_text_from_pdf_skips_empty_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _FakeReader:
        def __init__(self, _stream):
            self.pages = [_FakePage("Content"), _FakePage(""), _FakePage(None)]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)

    result = extract_text_from_bytes(b"%PDF-fake", "application/pdf", "doc.pdf")
    assert result == "Content"


# ---------------------------------------------------------------------------
# extract_text_from_bytes — DOCX
# ---------------------------------------------------------------------------


def test_extract_text_from_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeParagraph:
        def __init__(self, text):
            self.text = text

    class _FakeDocument:
        def __init__(self, _stream):
            self.paragraphs = [
                _FakeParagraph("First paragraph"),
                _FakeParagraph(""),
                _FakeParagraph("Third paragraph"),
            ]

    monkeypatch.setattr("docx.Document", _FakeDocument)

    result = extract_text_from_bytes(
        b"docx-bytes",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc.docx",
    )
    assert result == "First paragraph\n\nThird paragraph"


# ---------------------------------------------------------------------------
# extract_text_from_bytes — XLSX
# ---------------------------------------------------------------------------


def test_extract_text_from_xlsx(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeWorksheet:
        def iter_rows(self, values_only=True):
            return [("Name", "Age"), ("Alice", 30), (None, None)]

    class _FakeWorkbook:
        sheetnames = ["Sheet1"]

        def __getitem__(self, name):
            return _FakeWorksheet()

        def close(self):
            pass

    def _fake_load_workbook(_stream, read_only=True, data_only=True):
        return _FakeWorkbook()

    monkeypatch.setattr("openpyxl.load_workbook", _fake_load_workbook)

    result = extract_text_from_bytes(
        b"xlsx-bytes",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "data.xlsx",
    )
    assert "--- Sheet: Sheet1 ---" in result
    assert "Name\tAge" in result
    assert "Alice\t30" in result
