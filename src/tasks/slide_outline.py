from src.celery_app import celery
import json
import asyncio
from src.infra.messaging.redis_client import get_redis_client

from src.infra.utils.logger import get_logger
from sqlmodel import select
from src.infra.utils.database import get_master_session
from src.domain.models.organisation_tables import ChatMessage
from src.service.technical_deck.generate_slide_outline.slide_outline_workflow import SlideContentGenerator

logger = get_logger()
WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_slide_content"  # Keep as is for compatibility (matches event in websocket handler)
REDIS_CHANNEL = "websocket_messages"


def publish_websocket_message(sid, event, data):
    """Publish websocket message to Redis channel"""
    try:
        redis_client = get_redis_client()
        message = {"sid": sid, "event": event, "data": data}
        redis_client.publish(REDIS_CHANNEL, json.dumps(message))
        logger.info(f"Published message to Redis channel {REDIS_CHANNEL}")
    except Exception as e:
        logger.error(f"Error publishing websocket message: {e}")


@celery.task(bind=True)
def generate_slide_outline_task(self, sid, data_json):
    data = json.loads(data_json)
    session_id = data.get("session_id")
    message_id = data.get("message_id")
    user_id = data.get("user_id")
    organisation_id = data.get("organisation_id")
    logger.info(
        f"Celery task generate_slide_outline_task started: sid={sid}, session_id={session_id}, message_id={message_id}"
    )

    master_session = next(get_master_session())
    try:
        chat_message = master_session.exec(
            select(ChatMessage).where((ChatMessage.session_id == session_id) & (ChatMessage.message_id == message_id))
        ).first()
        if not chat_message:
            raise RuntimeError("Chat message not found")

        details = chat_message.technical_deck_details or {}
        input_summary = details.get("complete_transcript_summary")
        if not input_summary:
            raise RuntimeError("Assistant message lacks `complete_transcript_summary`.")

        agent = SlideContentGenerator(provider="azure", model_name="o3-mini")
        logger.info("Initialized SlideContentGenerator workflow.")

        async def _run_workflow():
            full_response = []
            solution_narration = None
            async for chunk in agent.run_workflow(input_summary=input_summary):
                if chunk.content_type == "SOLUTION_OUTLINE":
                    if chunk.content is not None:
                        full_response.append(chunk.content)
                        response_data = {
                            "chat_id": session_id,
                            "message_id": message_id,
                            "isError": False,
                            "message_type": "TECHNICAL_DECK",
                            "response": chunk.content,
                        }
                        publish_websocket_message(sid, RESPONSE_EVENT, response_data)
                elif chunk.content_type == "SOLUTION_NARRATION":
                    solution_narration = chunk.content
                    logger.info(f"Solution Narration Generated")
            return full_response, solution_narration

        full_response, solution_narration = asyncio.run(_run_workflow())
        logger.info("Slide outline generation completed")

        # Save to database
        details["state"] = "SLIDE_CONTENT"
        chat_message.technical_deck_details = {
            **details,
            "slide_content_summary": full_response,
            "solution_narration": solution_narration,
        }
        master_session.add(chat_message)
        master_session.commit()
        logger.info("Slide content saved to DB")

        # Emit completion event
        completion_data = {
            "chat_id": session_id,
            "message_id": message_id,
            "message_type": "TECHNICAL_DECK",
            "isComplete": True,
        }
        publish_websocket_message(sid, RESPONSE_EVENT, completion_data)

    except Exception as e:
        logger.exception("Error in Celery generate_slide_outline_task")
        error_data = {
            "chat_id": session_id,
            "message_id": message_id,
            "isError": True,
            "errorResponse": str(e),
            "message_type": "TECHNICAL_DECK",
        }
        publish_websocket_message(sid, RESPONSE_EVENT, error_data)
    finally:
        master_session.close()
