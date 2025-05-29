# app_layer/websockets/chat_message_handler.py

from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.chat_socket_models import ChatMessageReceived
import uuid

# import your DB and models
from sqlmodel import select
from models.chat_db_models import ChatMessage
from core.database import get_master_session
from core.redis_client import get_socket_connection
from workflows.general_chat.chat_workflow import GeneralChatAgent
import json

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_chat_message"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def send_chat_messages(
    sid,
    data: ChatMessageReceived,
):
    """
    1. Validate the incoming payload
    2. Create & save a ChatMessage
    3. Broadcast the new message to everyone in that session's room
    """
    try:
        logger.info(f"WebSocket message received: sid={sid}, data={data}")

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
        session_gen = get_master_session()
        master_session = next(session_gen)

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
        response_data = {
            "chat_id": chat_id,
            "message_id": system_message_id,
            "isComplete": False,
            "isLineComplete": False,
            "isError": False,
            "response": "",
        }
        full_response = ""

        # Getting list of documents from the chat messsage to send to the agent and take only last 6 messages
        statement = (
            select(ChatMessage).where(ChatMessage.session_id == chat_id).order_by(ChatMessage.timestamp.desc()).limit(6)
        )
        results = master_session.exec(statement)
        chat_messages = results.all()

        # Reverse to have most recent message at the bottom
        chat_messages = list(reversed(chat_messages))

        # Getting Response from Agent
        agent_connector = GeneralChatAgent(
            provider="azure", model_name="o3-mini", sio=sio, websocket_room=sid, system_message_id=system_message_id
        )

        # Emit response during chat agent processing
        async for response_chunk in agent_connector.run_workflow(message_list=chat_messages):
            if response_chunk.content is not None:
                full_response += response_chunk.content
                response_data.update({"response": response_chunk.content, "isLineComplete": "\n" in response_chunk.content})
                await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)

        assistant_msg = ChatMessage(
            session_id=chat_id,
            message_id=system_message_id,
            role="assistant",
            content=full_response,
            attachments=None,
        )
        logger.info("Message Saved Successfully")
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"General Message Flow Completed")

    except Exception as e:
        logger.exception("Error in send_chat_messages")
        response_data.update({"errorResponse": "Failed to generate chat response.", "isError": True})
    finally:
        response_data.update({"isComplete": True, "response": ""})
        await sio.emit(RESPONSE_EVENT, response_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        try:
            session_gen.close()
            master_session.close()
        except Exception:
            pass
