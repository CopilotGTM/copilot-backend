# app_layer/websockets/transcript_summary_handler.py
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.chat_socket_models import ChatMessageReceived
import uuid
import json
from typing import Dict, List

from sqlmodel import select
from models.chat_db_models import ChatMessage
from core.database import get_master_session
from core.redis_client import get_socket_connection
from models.websocket_response import WebSocketResponse

from workflows.transcript_summary.transcript_workflow import TranscriptSummaryAgent

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_transcript_messages"
CHAT_RESPONSE_EVENT = "recieve_chat_message"


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
    system_message_id = None
    master_session = None

    try:
        logger.info(f"Transcript Summary WebSocket message received: sid={sid}, data={data}")

        # Auth / connection details
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

        # Extract fields from your payload
        chat_id = data.get("session_id")
        master_session = next(get_master_session())

        # Save user message in DB
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

        # Save Assistant Message in DB
        system_message_id = str(uuid.uuid4())
        assistant_msg = ChatMessage(
            session_id=chat_id,
            message_id=system_message_id,
            role="assistant",
            content="Technical Deck Generation",
            attachments=data.get("attachments"),
            message_type="TECHNICAL_DECK",
        )
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Assistant Message Stored {assistant_msg}")

        # Emit first message
        await WebSocketResponse.emit_first_message(
            room=sid,
            event=CHAT_RESPONSE_EVENT,
            chat_id=chat_id,
            message_id=system_message_id,
            response="",
            message_type="TECHNICAL_DECK",
        )

        full_response: str = ""

        # Setting up agent
        agent_connector = TranscriptSummaryAgent(provider="azure", model_name="o3-mini")

        async for response_chunk in agent_connector.run_workflow(attached_models=data.get("attachments")):
            if response_chunk.content is not None:
                full_response += response_chunk.content
                await WebSocketResponse.emit_chunk(
                    room=sid,
                    event=RESPONSE_EVENT,
                    chat_id=chat_id,
                    message_id=system_message_id,
                    response=response_chunk.content,
                    message_type="TECHNICAL_DECK"
                )

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

        assistant_msg: ChatMessage = master_session.exec(
            select(ChatMessage).where(
                (ChatMessage.session_id == chat_id) & (ChatMessage.message_id == system_message_id)
            )
        ).first()
        logger.info(assistant_msg)

        logger.info("Transcript Data Stored Prepared")
        if not assistant_msg.technical_deck_details:
            assistant_msg.technical_deck_details = {
                "transcript_summary_list": transcript_summary,
                "complete_transcript_summary": full_response,
                "state": "TRANSCRIPT_SUMMARY",
            }
        else:
            assistant_msg.technical_deck_details = {
                **assistant_msg.technical_deck_details,
                "transcript_summary_list": transcript_summary,
                "complete_transcript_summary": full_response,
                "state": "TRANSCRIPT_SUMMARY",
            }
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Transcript Data Stored")
        logger.info(f"Transcript Generation Completes")

    except Exception as e:
        logger.exception("Error in send_transript_messages")
        if system_message_id:
            await WebSocketResponse.emit_error(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=chat_id,
                message_id=system_message_id,
                error_message="Failed to generate transcript summary.",
                message_type="TECHNICAL_DECK"
            )

    finally:
        if system_message_id:
            await WebSocketResponse.emit_completion(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=chat_id,
                message_id=system_message_id,
                message_type="TECHNICAL_DECK"
            )
        # clean up DB session generator if we opened one
        try:
            if master_session is not None:
                master_session.close()
        except Exception:
            pass
