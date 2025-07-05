# app_layer/websockets/slide_content_generation.py
from src.api.events.socket_app import sio
from src.infra.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
from src.api.schemas.chat_socket_models import SlideContentGenerationModel
import json

from sqlmodel import select
from src.domain.models.chat_db_models import ChatMessage
from src.infra.utils.database import get_master_session
from src.infra.messaging.redis_client import get_socket_connection
from src.api.utils.websocket_response import WebSocketResponse

from src.service.technical_deck.generate_slide_outline.slide_outline_workflow import SlideContentGenerator
from src.tasks.slide_outline import generate_slide_outline_task
import json

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
    system_message_id = None
    chat_id = None

    try:
        logger.info(f"Slide Content Generation WebSocket message received: sid={sid}, data={data}")

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

        # Extract identifiers from request payload
        chat_id = data.get("session_id")
        system_message_id = data.get("message_id")
        
        # Prepare the task payload for Celery
        task_data = {
            "session_id": chat_id,
            "message_id": system_message_id,
            "user_id": user_id,
            "organisation_id": organisation_id,
        }

        # Kick off celery worker for slide outline generation
        generate_slide_outline_task.delay(sid, json.dumps(task_data))
        logger.info("Slide outline generation task started")

        # Emit ack/first message immediately
        await WebSocketResponse.emit_first_message(
            room=sid,
            event=RESPONSE_EVENT,
            chat_id=chat_id,
            message_id=system_message_id,
            response="",
            message_type="TECHNICAL_DECK",
        )

    except Exception as e:
        logger.exception("Error in generate_slide_content")
        if system_message_id and chat_id:
            await WebSocketResponse.emit_error(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=chat_id,
                message_id=system_message_id,
                error_message="Failed to generate slide content.",
                message_type="TECHNICAL_DECK",
            )
    # Cleanup or finally removed: DB handle not opened now
