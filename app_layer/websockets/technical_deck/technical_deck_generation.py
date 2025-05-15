# app_layer/websockets/transcript_summary_handler.py

from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.technical_deck_models import GenerateTechnicalDeckRequest
import json

from sqlmodel import select
from models.chat_db_models import ChatMessage as ChatMessageDB
from models.chat_db_models import ChatSession as ChatSessionDB

from core.database import get_master_session
from core.redis_client import get_socket_connection

from workflows.deck_generation.deck_generation_workflow import DeckGeneration

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "receive_generated_deck"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def generate_deck(
    sid,
    data: GenerateTechnicalDeckRequest,
):

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
        session_id = data.get("session_id")
        master_session = next(get_master_session())
        system_message_id = data.get("message_id")
        full_response = []
        response_data = {
            "chat_id": session_id,
            "message_id": system_message_id,
            "isComplete": False,
            "isLineComplete": False,
            "isError": False,
            "response": "",
        }

        # Fetch the chat session to verify
        statement = select(ChatSessionDB).where(
            ChatSessionDB.session_id == session_id,
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted == False,
        )
        chat = master_session.exec(statement).one_or_none()

        if not chat:
            logger.error(f"Chat session not found for user/org: {user_id}/{organisation_id}")
            response_data.update(
                {
                    "errorResponse": "Chat session not found",
                    "isError": True,
                }
            )
            await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
            return

        # Read the Message from chat message using session_id and message_id
        logger.info(f"Fetching chat message for session_id: {session_id}")
        deck_message: ChatMessageDB = master_session.exec(
            select(ChatMessageDB).where(
                (ChatMessageDB.session_id == session_id) & (ChatMessageDB.message_id == system_message_id)
            )
        ).first()

        if not deck_message:
            logger.error(f"No assistant ChatMessage found for session_id={session_id}")
            response_data.update(
                {
                    "errorResponse": "No assistant ChatMessage found",
                    "isError": True,
                }
            )
            await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
            return

        # Setting up agent
        agent_connector = DeckGeneration(provider="azure", model_name="o3-mini")

        async for response_chunk in agent_connector.run_workflow():
            full_response.append(response_chunk.content)
            response_data.update({"response": response_chunk.content})
            await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

        # Update the deck_message with generated technical deck details
        deck_message.technical_deck_details = {
            **deck_message.technical_deck_details,  # Preserve existing details
            "technical_deck": full_response,  # Add generated deck content
            "state": "SLIDE_OUTPUT",  # Update state to indicate completion
        }
        # Save changes to database
        master_session.add(deck_message)
        master_session.commit()
        master_session.refresh(deck_message)
        logger.info("Technical deck generation completed successfully")
    except Exception as e:
        logger.exception("Error during technical deck generation")
        # Send error status to client with descriptive message
        response_data.update(
            {
                "errorResponse": "Failed to generate technical deck. Please try again later.",
                "isError": True,
            }
        )
        await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

    finally:
        # Send final completion status to client
        response_data.update({"isComplete": True, "response": ""})
        await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        # Cleanup database session
        try:
            if master_session is not None:
                master_session.close()
        except Exception:
            logger.warning("Failed to close database session")
            pass
