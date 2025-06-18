# app_layer/websockets/chat_message_handler.py

# Standard library imports
import json
import uuid
from typing import Any, Dict

# Third-party imports
from socketio.exceptions import ConnectionRefusedError
from sqlmodel import select

# Local application imports
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from core.database import get_master_session
from core.redis_client import get_socket_connection
from models.chat_db_models import ChatMessage
from models.chat_socket_models import ChatMessageReceived
from models.websocket_response import WebSocketResponse
from workflows.general_chat.chat_workflow import GeneralChatAgent


logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_chat_message"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def send_chat_messages(sid: str, data: ChatMessageReceived) -> None:
    """Handle incoming chat messages and generate responses.

    This function:
    1. Validates the incoming payload
    2. Creates & saves a ChatMessage
    3. Broadcasts the new message to everyone in that session's room

    Args:
        sid: Socket ID of the client
        data: Chat message data received from the client

    Raises:
        ConnectionRefusedError: If authentication fails or connection details are invalid
    """
    chat_id = None
    system_message_id = None
    session_gen = None
    master_session = None

    try:
        logger.info(f"WebSocket message received: sid={sid}")

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

        # Extract fields from payload
        chat_id = data.get("session_id")
        session_gen = get_master_session()
        master_session = next(session_gen)

        # Create and save user message
        chat_message = ChatMessage(
            session_id=chat_id,
            message_id=data.get("message_id"),
            role="user",
            content=data.get("content", ""),
            attachments=data.get("attachments"),
        )
        master_session.add(chat_message)
        master_session.commit()
        master_session.refresh(chat_message)

        system_message_id = str(uuid.uuid4())
        full_response = ""

        # Get last 6 messages for context
        statement = (
            select(ChatMessage).where(ChatMessage.session_id == chat_id).order_by(ChatMessage.timestamp.desc()).limit(6)
        )
        results = master_session.exec(statement)
        chat_messages = list(reversed(results.all()))  # Reverse to have most recent message at bottom

        # Initialize and run chat agent
        agent_connector = GeneralChatAgent(
            provider="azure", model_name="o3-mini", sio=sio, websocket_room=sid, system_message_id=system_message_id
        )

        # Process and emit response chunks
        async for response_chunk in agent_connector.run_workflow(message_list=chat_messages):
            if response_chunk.content is not None:
                full_response += response_chunk.content
                await WebSocketResponse.emit_chunk(
                    room=sid,
                    event=RESPONSE_EVENT,
                    chat_id=chat_id,
                    message_id=system_message_id,
                    response=response_chunk.content,
                    message_type="GENERAL_CHAT",
                )

        # Save assistant's response
        assistant_msg = ChatMessage(
            session_id=chat_id,
            message_id=system_message_id,
            role="assistant",
            content=full_response,
            attachments=None,
        )
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info("General Message Flow Completed")

    except Exception as e:
        logger.exception("Error in send_chat_messages")
        if chat_id and system_message_id:
            await WebSocketResponse.emit_error(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=chat_id,
                message_id=system_message_id,
                error_message="Failed to generate chat response.",
                message_type="GENERAL_CHAT",
            )

    finally:
        if chat_id and system_message_id:
            await WebSocketResponse.emit_completion(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=chat_id,
                message_id=system_message_id,
                message_type="GENERAL_CHAT",
            )

        # Clean up database connections
        if session_gen:
            try:
                session_gen.close()
            except Exception:
                pass

        if master_session:
            try:
                master_session.close()
            except Exception:
                pass
