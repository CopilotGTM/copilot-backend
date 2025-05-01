# app_layer/websockets/transcript_summary_handler.py
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.chat_socket_models import ChatMessageReceived
import uuid
import json
from typing import Dict, Any, List

# ─── DB + models ────────────────────────────────────────────────────────────────
from sqlmodel import Session, select
from models.chat_db_models import ChatSession, ChatMessage, AttachmentModel
from core.database import get_master_session
from core.redis_client import get_socket_connection

from workflows.transcript_summary.transcript_workflow import TranscriptSummaryAgent

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_transcript_messages"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def send_transript_messages(
    sid,
    data: ChatMessageReceived,
):
    """
    1. Validate the incoming payload
    2. Persist the user ChatMessage (+ transcript in JSONB)
    3. Stream the agent response
    4. Persist the assistant ChatMessage (+ transcript_summary in JSONB)
    """
    session_gen = None
    master_session = None
    try:
        logger.info("--------------------------------------------------------------------")
        logger.info(f"WebSocket message received: sid={sid}, data={data}")

        # ─── 1. Auth / connection details ───────────────────────────────────────
        connection_details = await get_socket_connection(sid)
        if not connection_details:
            logger.warning(f"Connection details not found for sid: {sid}")
            raise ConnectionRefusedError("Unauthorized")

        # Extract user_id and organisation_id from the connection details
        connection_details = json.loads(connection_details)
        user_id = connection_details.get("user_id")
        organisation_id = connection_details.get("organisation_id")
        if not user_id or not organisation_id:
            logger.error("Connect refused: token missing user_id/organisation_id")
            raise ConnectionRefusedError("Invalid authentication payload")

        # ─── 2. Save USER message ------------------------------------------------
        chat_id = data.get("session_id")
        session_gen = get_master_session()
        master_session = next(session_gen)

        user_msg = ChatMessage(
            session_id=chat_id,
            message_id=data.get("message_id"),
            role="user",
            content=data.get("content", ""),
            attachments=data.get("attachments"),
            message_type=data.get("message_type", "TECHNICAL_DECK"),
        )
        master_session.add(user_msg)
        master_session.commit()
        master_session.refresh(user_msg)

        # ─── 3. Stream assistant response ---------------------------------------
        system_message_id = str(uuid.uuid4())
        response_data = {
            "chat_id": chat_id,
            "message_id": system_message_id,
            "isComplete": False,
            "isLineComplete": False,
            "isError": False,
            "response": "",
        }
        full_response: str = ""

        # Getting Response from Agent
        agent_connector = TranscriptSummaryAgent(provider="azure", model_name="o3-mini")

        for response_chunk in agent_connector.run(attached_models=data.get("attachments")):
            full_response += response_chunk.content
            logger.info(f"Response chunk: {response_chunk.content}")
            response_data.update({"response": response_chunk.content, "isLineComplete": "\n" in response_chunk.content})
            await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

        # ─── 4. Save ASSISTANT message ------------------------------------------
        # Build transcript_summary list: [{'url': <file>, 'summary': <text>}]
        transcript_summary: List[Dict[str, str]] = []
        for att in data.get("attachments", []):
            url = att.get("url") if isinstance(att, dict) else att.url
            transcript_summary.append(
                {
                    "url": url,
                    "summary": full_response,
                }
            )
        logger.info(f"Transcript Data Stored Prepared")
        assistant_msg = ChatMessage(
            session_id=chat_id,
            message_id=system_message_id,
            role="assistant",
            content=full_response,
            attachments=None,
            message_type="TECHNICAL_DECK",
            technical_deck_details={
                "transcript_summary_list": transcript_summary,
                "complete_transcript_summary": full_response,
            },
        )
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Transcript Data Stored")
        logger.info(f"Transcript Generation Completes")
    except Exception as e:
        logger.exception("Error in send_transript_messages")
        # send error status to client
        response_data.update(
            {
                "errorResponse": "Failed to generate chat response.",
                "isError": True,
            }
        )
        await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

    finally:
        response_data.update({"isComplete": True, "response": ""})
        await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        # clean up DB session generator if we opened one
        try:
            if session_gen is not None:
                session_gen.close()
            if master_session is not None:
                master_session.close()
        except Exception:
            pass
