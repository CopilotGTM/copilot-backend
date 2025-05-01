# app_layer/websockets/slide_content_generation.py
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.chat_socket_models import SlideContentGenerationModel
import uuid
import json
from typing import Dict, Any, List

from sqlmodel import Session, select
from models.chat_db_models import ChatSession, ChatMessage, AttachmentModel
from core.database import get_master_session
from core.redis_client import get_socket_connection

from workflows.generate_slide_content.slide_content_workflow import SlideContentGenerator

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_slide_content"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def generate_slide_content(
    sid,
    data: SlideContentGenerationModel,
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

        # Auth / connection details
        logger.info(f"Extracting connection details for sid: {sid}")
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

        # Getting Database Connection
        logger.info("Getting DB connection")
        chat_id = data.get("session_id")
        system_message_id = data.get("message_id")
        session_gen = get_master_session()
        master_session = next(session_gen)

        # Read the Message from chat message using chat_id
        logger.info(f"Fetching chat message for session_id: {chat_id}")
        existing_assistant: ChatMessage = master_session.exec(
            select(ChatMessage).where(
                (ChatMessage.session_id == chat_id) & (ChatMessage.message_id == system_message_id)
            )
        ).first()

        if not existing_assistant:
            logger.error(f"No assistant ChatMessage found for session_id={chat_id}")
            raise RuntimeError(f"No assistant ChatMessage found for session_id={chat_id}")

        details = existing_assistant.technical_deck_details or {}
        input_summary = details.get("complete_transcript_summary")
        logger.info(f"Input summary: {input_summary}")
        if not input_summary:
            logger.error(f"Assistant message for session_id={chat_id} lacks `complete_transcript_summary`.")
            raise RuntimeError(f"Assistant message for session_id={chat_id} " "lacks `complete_transcript_summary`.")

        # Response formats
        logger.info("Preparing response formats")
        response_data = {
            "chat_id": chat_id,
            "message_id": system_message_id,
            "isComplete": False,
            "isLineComplete": False,
            "isError": False,
            "response": "",
        }
        full_response: list = []

        # Getting Response from Agent
        logger.info("Generating slide content")
        agent_connector = SlideContentGenerator(provider="azure", model_name="o3-mini")

        for response_chunk in agent_connector.run(input_summary=input_summary):
            full_response.append(response_chunk.content)
            logger.info(f"Response chunk: {response_chunk.content}")
            response_data.update({"response": response_chunk.content})
            await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

        # Saving slide content to DB
        existing_assistant.technical_deck_details = {**details, "slide_content_summary": full_response}
        logger.info(f"Transcript Data Stored Prepared")
        master_session.add(existing_assistant)
        master_session.commit()
        master_session.refresh(existing_assistant)
        logger.info("Slide-content saved back to original assistant message")
        logger.info(f"Slide content saved to DB")
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
