# app_layer/routers/transcript_format.py
"""
POST /parse_transcript
======================

Body (application/json)

{
  "url":  "s3://my-bucket/meetings/March_28.docx",   # any fsspec-supported URL
  "file_name": "March_28.docx",                          # optional – inferred if omitted
  "file_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}

↳ 200 OK → meeting-JSON
"""

from __future__ import annotations

import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

import fsspec
import docx

# import pdfplumber
from fastapi import APIRouter, Depends, HTTPException, status

from core.dto.authentication import Authentication
from app_layer.utils.logger import get_logger
from models.chat_socket_models import AttachmentModel

# ── FastAPI plumbing ─────────────────────────────────────────────────────
auth = Authentication()
transcript_router = APIRouter()
logger = get_logger()

# ── limits / constants ───────────────────────────────────────────────────
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSIONS = {".docx", ".pdf"}

HEADERS = ["Outcome", "Next Steps", "Pain Points", "Interests", "Key Takeaways", "Transcript"]
WANTED_SECTIONS = ["Outcome", "Next Steps", "Transcript"]

HEADER_RE = re.compile(rf"({'|'.join(re.escape(h) + r':?' for h in HEADERS)})", re.I)
LINE_RE = re.compile(r"(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?([\w\s]+):\s*(.*)")


# ── helpers: reading files via fsspec ─────────────────────────────────────
def _check_size(fs: fsspec.AbstractFileSystem, path: str) -> None:
    try:
        size = fs.info(path).get("size", 0)
        if size and size > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")


def _load_docx(path: str) -> str:
    with fsspec.open(path, "rb") as fp:
        d = docx.Document(fp)
    return "\n".join(p.text.strip() for p in d.paragraphs if p.text.strip())


# def _load_pdf(path: str) -> str:
#     with fsspec.open(path, "rb") as fp:
#         with pdfplumber.open(fp) as pdf:
#             pages = [p.extract_text() or "" for p in pdf.pages]
#     return "\n".join(pages)


def path_to_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    fs, fs_path = fsspec.core.url_to_fs(path)
    _check_size(fs, fs_path)

    if ext == ".docx":
        return _load_docx(path)
    # if ext == ".pdf":
    #     return _load_pdf(path)
    raise HTTPException(status_code=400, detail="Only .docx or .pdf supported")


# ── helpers: transcript parsing ──────────────────────────────────────────
def split_by_headers(text: str) -> Dict[str, str]:
    parts = HEADER_RE.split(text)
    sections: Dict[str, str] = {}
    for i in range(1, len(parts), 2):
        name = parts[i].rstrip(":").strip()
        sections[name] = parts[i + 1].strip() if i + 1 < len(parts) else ""
    return sections


def parse_transcript(block: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    last_ts = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if m:
            ts, spk, content = m.groups()
            last_ts = ts or last_ts
            out.append({"Speaker": spk.strip(), "Content": content.strip(), "timestamp": last_ts})
        elif out:
            out[-1]["Content"] += " " + line
    return out


def build_json(text: str) -> Dict[str, Any]:
    mt = re.search(r"Meeting Title:\s*(.*)", text)
    title = mt.group(1).strip() if mt else text.splitlines()[0][:60]

    sections = split_by_headers(text) or {"Transcript": text}
    result: Dict[str, Any] = {"Meeting Title": title, "Sections": []}

    for name in WANTED_SECTIONS:
        if name == "Transcript" and "Transcript" in sections:
            dialogs = parse_transcript(sections["Transcript"])
        else:
            body = sections.get(name, "")
            dialogs = [{"Speaker": "System", "Content": body, "timestamp": ""}] if body else []
        if dialogs:
            result["Sections"].append({"Section Name": name, "Dialogues": dialogs})

    if not any(s["Section Name"] == "Transcript" for s in result["Sections"]):
        result["Sections"].append({"Section Name": "Transcript", "Dialogues": parse_transcript(text)})
    return result


# ── API Endpoint ─────────────────────────────────────────────────────────
@transcript_router.post("/parse_transcript", status_code=200, tags=["transcript"])
def parse_transcript_api(
    attachment: AttachmentModel, auth_data: Dict[str, Any] = Depends(auth.verify_session)
) -> Dict[str, Any]:
    """
    Fetch a .docx or .pdf from any fsspec-supported URL, parse it, and
    return structured meeting JSON.
    """
    url = attachment.url
    filename = attachment.filename
    ext = os.path.splitext(filename)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {ext}. Only .docx or .pdf allowed")

    logger.info(f"Parsing transcript from: {url}")

    try:
        plain = path_to_text("x-local-test\Harika Discussion.docx")
        payload = build_json(plain)
        logger.info(f"Successfully parsed transcript {filename}")
        logger.info(f"Parsed JSON: {payload}")
        return payload

    except HTTPException:
        raise  # already has proper status & message
    except Exception as exc:
        logger.exception("Transcript parsing failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to parse transcript: {exc}"
        )
