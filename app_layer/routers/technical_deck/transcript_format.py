# app_layer/routers/transcript_format.py
from __future__ import annotations

import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query

from core.dto.authentication import Authentication
from app_layer.utils.logger import get_logger
from models.chat_models import ParseTranscript
from infrastructure.document_reader import DocumentReader
from infrastructure.cloud_file_reader import CloudConfig

from sqlmodel import Session, select
from models.chat_db_models import ChatSession, ChatMessage, AttachmentModel
from core.database import get_master_session
from models.http_response import Response

auth = Authentication()
transcript_router = APIRouter()
logger = get_logger()

CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER")
APPLICATION_STORAGE_NAME = os.getenv("APPLICATION_STORAGE_NAME")
APPLICATION_REGION = os.getenv("APPLICATION_REGION")


MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSIONS = {".docx", ".pdf"}

HEADERS = ["Outcome", "Next Steps", "Pain Points", "Interests", "Key Takeaways", "Transcript"]
WANTED_SECTIONS = ["Outcome", "Next Steps", "Transcript"]

HEADER_RE = re.compile(rf"({'|'.join(re.escape(h) + r':?' for h in HEADERS)})", re.I)
LINE_RE = re.compile(r"(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?([\w\s]+):\s*(.*)")


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
            out.append({"speaker": spk.strip(), "content": content.strip(), "timestamp": last_ts})
        elif out:
            out[-1]["content"] += " " + line
    return out


def build_json(text: str) -> Dict[str, Any]:
    mt = re.search(r"Meeting Title:\s*(.*)", text)
    title = mt.group(1).strip() if mt else text.splitlines()[0][:60]

    sections = split_by_headers(text) or {"Transcript": text}
    result: Dict[str, Any] = {"meeting_title": title, "sections": []}

    for name in WANTED_SECTIONS:
        if name == "Transcript" and "Transcript" in sections:
            dialogs = parse_transcript(sections["Transcript"])
        else:
            body = sections.get(name, "")
            dialogs = [{"speaker": "System", "content": body, "timestamp": ""}] if body else []
        if dialogs:
            result["sections"].append({"section_name": name, "dialogues": dialogs})

    if not any(s["section_name"] == "Transcript" for s in result["sections"]):
        result["sections"].append({"section_name": "Transcript", "dialogues": parse_transcript(text)})
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
        file_url = str(attachment.get("url"))
        filename = attachment.get("filename")
        ext = os.path.splitext(filename)[1].lower()

        logger.info(f"Attachment Data {attachments}")

        if ext not in ALLOWED_EXTENSIONS:
            return Response.bad_request_error_response(f"Unsupported file extension: {ext}", data=None)

        logger.info(f"Parsing transcript from: {file_url}")

        try:
            # Initialize cloud-agnostic document reader
            cloud_config = CloudConfig(CLOUD_PROVIDER, APPLICATION_REGION)
            document_reader = DocumentReader(cloud_config)

            # Read and process the document
            plain = document_reader.read_document(file_url)
            payload = build_json(plain)
            logger.info(f"Successfully parsed transcript {filename}")

            formatted_transcript = {"formatted_transcript": [payload]}

            technical_deck.technical_deck_details = {**deck_details, "formatted_transcript": [payload]}
            logger.info(f"Update Formatted Transcript")
            master_session.add(technical_deck)
            master_session.commit()
            master_session.refresh(technical_deck)
            return Response.success_response("Successfully parsed transcript", formatted_transcript)
        except ValueError as e:
            logger.error(f"Error processing document: {str(e)}")
            return Response.bad_request_error_response(str(e), data=None)

    except Exception as exc:
        logger.exception("Transcript parsing failed")
        return Response.internal_server_error_response(str(exc), data=None)
