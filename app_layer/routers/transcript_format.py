# app_layer/routers/transcript_format.py
from __future__ import annotations

import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional
from uuid import UUID

import fsspec
import docx

# import pdfplumber
from fastapi import APIRouter, Depends, HTTPException, status, Query

from core.dto.authentication import Authentication
from app_layer.utils.logger import get_logger
from models.chat_models import ParseTranscript
from workflows.utils.cloud_uri import to_s3_uri

from sqlmodel import Session, select
from models.chat_db_models import ChatSession, ChatMessage, AttachmentModel
from core.database import get_master_session
from models.http_response import Response

auth = Authentication()
transcript_router = APIRouter()
logger = get_logger()

MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSIONS = {".docx", ".pdf"}

HEADERS = ["Outcome", "Next Steps", "Pain Points", "Interests", "Key Takeaways", "Transcript"]
WANTED_SECTIONS = ["Outcome", "Next Steps", "Transcript"]

HEADER_RE = re.compile(rf"({'|'.join(re.escape(h) + r':?' for h in HEADERS)})", re.I)
LINE_RE = re.compile(r"(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?([\w\s]+):\s*(.*)")


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


@transcript_router.get("/parse_transcript", status_code=200, tags=["transcript"])
def parse_transcript_api(
    session_id: UUID = Query(..., description="Conversation thread ID"),
    teachnical_deck_message_id: UUID = Query(..., description="Unique message ID"),
    # auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Fetch a .docx or .pdf from any fsspec-supported URL, parse it, and
    return structured meeting JSON.
    """
    try:
        logger.info(f"Working on Transcript Formatting")
        # Fetching Technical Deck Message Details
        technical_deck: ChatMessage = master_session.exec(
            select(ChatMessage).where(
                (ChatMessage.session_id == session_id) & (ChatMessage.message_id == teachnical_deck_message_id)
            )
        ).first()
        logger.info(f"Technical Deck Message Details Fetched")

        # Getting attachments and technical deck
        attachments = technical_deck.attachments
        deck_details = technical_deck.technical_deck_details
        if not attachments:
            logger.warning(f"No attachments or deck details found for session_id={session_id}")
            return Response.not_found_error_response(
                f"No attachments or deck details found for session_id={session_id}", data=None
            )

        attachment = attachments[0]
        url = str(attachment.get("url"))
        filename = attachment.get("filename")
        ext = os.path.splitext(filename)[1].lower()

        logger.info(f"Attachment Data {attachments}")

        if ext not in ALLOWED_EXTENSIONS:
            return Response.bad_request_error_response(f"Unsupported file extension: {ext}", data=None)

        logger.info(f"Parsing transcript from: {url}")

        transcript_file_path = to_s3_uri(url)
        plain = path_to_text(transcript_file_path)
        payload = build_json(plain)
        logger.info(f"Successfully parsed transcript {filename}")

        fomratted_transcript = {"fomratted_transcript": [payload]}

        technical_deck.technical_deck_details = {**deck_details, "fomratted_transcript": [payload]}
        logger.info(f"Update Formatted Transcript")
        master_session.add(technical_deck)
        master_session.commit()
        master_session.refresh(technical_deck)
        return Response.success_response("Successfully parsed transcript", fomratted_transcript)
    except Exception as exc:
        logger.exception("Transcript parsing failed")
        return Response.internal_server_error_response(str(exc), data=None)
