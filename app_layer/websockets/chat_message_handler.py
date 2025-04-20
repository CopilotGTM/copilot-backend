# app_layer/websockets/chat_message_handler.py

from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from models.chat_socket_models import ChatMessageReceived
from fastapi import Depends
# import your DB and models
from sqlmodel import Session, select
from models.chat_db_models import ChatSession, ChatMessage
from core.database import get_master_session
from core.redis_client import get_socket_connection

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"

@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def send_chat_messages(sid, data: ChatMessageReceived,
    master_session: Session = Depends(get_master_session),
):
    """
    1. Validate the incoming payload
    2. Create & save a ChatMessage
    3. Broadcast the new message to everyone in that session’s room
    """
    try:
        logger.info(f"WebSocket message received: sid={sid}, data={data}")
        # Validate the incoming payload
        connection_details = await get_socket_connection(sid)
        if not connection_details:
            logger.warning(f"Connection details not found for sid: {sid}")
            raise ConnectionRefusedError("Unauthorized")
        
        # Extract user_id and org_id from the connection details
        environ = connection_details.get("environ")
        user_id = environ.get("user_id")
        org_id = environ.get("organisation_id")

        if not user_id or not org_id:
            logger.error("Connect refused: token missing user_id/org_id")
            raise ConnectionRefusedError("Invalid authentication payload")

        # Extract fields from your payload
        chat_id = data.session_id
        message = data.message

        chat_message = ChatMessage(
            session_id=chat_id,
            message_id=message.message_id,
            role=message.role,
            content=message.content,
            attachments=message.attachments,
        )
        master_session.add(chat_message)
        master_session.commit()
        master_session.refresh(chat_message)


    except Exception as e:
        logger.exception("Error in send_chat_messages")
        # Let the client know something went wrong
        await sio.emit(
            "error",
            {"msg": "could not send message"},
            to=sid,
            namespace=WEBSOCKET_NAMESPACE,
        )
