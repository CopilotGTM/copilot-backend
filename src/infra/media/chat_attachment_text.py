"""Shared helpers for extracting text from chat attachment bytes."""

from __future__ import annotations

import io

IMAGE_MIMETYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}

AUDIO_MIMETYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/ogg",
    "audio/webm",
    "audio/flac",
    "audio/aac",
    "audio/amr",
    "audio/m4a",
    "audio/mp4",
    "audio/x-m4a",
}


def is_image_mimetype(mimetype: str) -> bool:
    return (mimetype or "").lower() in IMAGE_MIMETYPES


def is_audio_mimetype(mimetype: str) -> bool:
    return (mimetype or "").lower() in AUDIO_MIMETYPES


def _append_bounded(
    parts: list[str],
    text: str,
    *,
    separator: str,
    max_chars: int | None,
) -> bool:
    candidate = f"{separator if parts else ''}{text}"
    if max_chars is None:
        parts.append(candidate)
        return True

    # copilotgtm: output is capped at 12k chars; carry a running length if that ceiling grows.
    remaining = max_chars - sum(len(part) for part in parts)
    if remaining <= 0:
        return False
    parts.append(candidate[:remaining])
    return len(candidate) <= remaining


def extract_text_from_bytes(
    content: bytes,
    mimetype: str,
    _filename: str,
    *,
    max_chars: int | None = None,
) -> str:
    """Extract text from document bytes based on mimetype."""
    normalized = (mimetype or "").lower()

    if normalized == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text and not _append_bounded(pages, text, separator="\n\n", max_chars=max_chars):
                break
        return "".join(pages)

    if normalized == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document

        doc = Document(io.BytesIO(content))
        paragraphs: list[str] = []
        for paragraph in doc.paragraphs:
            if paragraph.text.strip() and not _append_bounded(
                paragraphs, paragraph.text, separator="\n\n", max_chars=max_chars
            ):
                break
        return "".join(paragraphs)

    if normalized == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(io.BytesIO(content))
        parts: list[str] = []
        limit_reached = False
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_started = False

            def add_slide_text(text: str, slide_number: int = slide_num) -> bool:
                """Append a slide fragment and report whether the limit still has room."""
                nonlocal slide_started
                if not slide_started:
                    slide_started = True
                    if not _append_bounded(
                        parts,
                        f"--- Slide {slide_number} ---",
                        separator="\n",
                        max_chars=max_chars,
                    ):
                        return False
                return _append_bounded(parts, text, separator="\n", max_chars=max_chars)

            for shape in slide.shapes:
                shape_texts: list[str] = []
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    shape_texts.extend(
                        child.text.strip()
                        for child in shape.shapes
                        if hasattr(child, "text") and child.text.strip()
                    )
                elif shape.has_table:
                    shape_texts.extend(
                        "\t".join(cell.text.strip() for cell in row.cells)
                        for row in shape.table.rows
                    )
                elif hasattr(shape, "text") and shape.text.strip():
                    shape_texts.append(shape.text.strip())

                for shape_text in filter(str.strip, shape_texts):
                    if not add_slide_text(shape_text):
                        limit_reached = True
                        break
                if limit_reached:
                    break

            if not limit_reached and slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    limit_reached = not add_slide_text(f"[Notes] {notes.text.strip()}")
            if limit_reached:
                break
        return "".join(parts)

    if normalized == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet_parts: list[str] = []
        limit_reached = False
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                header_added = False
                for row in worksheet.iter_rows(values_only=True):
                    values = [str(v).strip() if v is not None else "" for v in row]
                    if not any(values):
                        continue
                    if not header_added:
                        header_added = True
                        if not _append_bounded(
                            sheet_parts,
                            f"--- Sheet: {sheet_name} ---",
                            separator="\n",
                            max_chars=max_chars,
                        ):
                            limit_reached = True
                            break
                    if not _append_bounded(
                        sheet_parts,
                        "\t".join(values),
                        separator="\n",
                        max_chars=max_chars,
                    ):
                        limit_reached = True
                        break
                if limit_reached:
                    break
        finally:
            workbook.close()
        return "".join(sheet_parts)

    if normalized.startswith("text/") or normalized in {
        "application/json",
        "application/xml",
        "application/x-ndjson",
        "application/ndjson",
        "application/jsonl",
    }:
        decoded = content.decode("utf-8", errors="replace")
        return decoded if max_chars is None else decoded[:max_chars]

    return ""
