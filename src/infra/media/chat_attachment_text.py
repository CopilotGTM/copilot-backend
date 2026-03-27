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
    "audio/m4a",
    "audio/mp4",
    "audio/x-m4a",
}


def is_image_mimetype(mimetype: str) -> bool:
    return (mimetype or "").lower() in IMAGE_MIMETYPES


def is_audio_mimetype(mimetype: str) -> bool:
    return (mimetype or "").lower() in AUDIO_MIMETYPES


def extract_text_from_bytes(content: bytes, mimetype: str, _filename: str) -> str:
    """Extract text from document bytes based on mimetype."""
    normalized = (mimetype or "").lower()

    if normalized == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)

    if normalized == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document

        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)

    if normalized == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(io.BytesIO(content))
        parts: list[str] = []
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_parts: list[str] = [f"--- Slide {slide_num} ---"]
            for shape in slide.shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for child in shape.shapes:
                        if hasattr(child, "text") and child.text.strip():
                            slide_parts.append(child.text.strip())
                elif shape.has_table:
                    for row in shape.table.rows:
                        row_text = "\t".join(c.text.strip() for c in row.cells)
                        if row_text.strip():
                            slide_parts.append(row_text)
                elif hasattr(shape, "text") and shape.text.strip():
                    slide_parts.append(shape.text.strip())
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    slide_parts.append(f"[Notes] {notes.text.strip()}")
            if len(slide_parts) > 1:
                parts.extend(slide_parts)
        return "\n".join(parts)

    if normalized == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet_parts: list[str] = []
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                rows: list[str] = []
                for row in worksheet.iter_rows(values_only=True):
                    values = [str(v).strip() if v is not None else "" for v in row]
                    if any(values):
                        rows.append("\t".join(values))
                if rows:
                    sheet_parts.append(f"--- Sheet: {sheet_name} ---")
                    sheet_parts.extend(rows)
        finally:
            workbook.close()
        return "\n".join(sheet_parts)

    if normalized.startswith("text/") or normalized in {
        "application/json",
        "application/xml",
        "application/x-ndjson",
        "application/ndjson",
        "application/jsonl",
    }:
        return content.decode("utf-8", errors="replace")

    return ""
