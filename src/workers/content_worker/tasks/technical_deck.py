from src.workers.content_worker.celery_app import app
import json
import asyncio
from src.infra.messaging.redis_client import get_redis_client

from src.infra.utils.logger import get_logger
from src.infra.utils.logger import get_logger as InputSio
from sqlmodel import select
from src.infra.utils.database import get_master_session
from src.domain.models.organisation_tables import (
    ChatSession as ChatSessionDB,
    ChatMessage as ChatMessageDB,
)
from src.service.technical_deck.deck_generation.deck_generation_workflow import (
    DeckGeneration,
)


logger = get_logger()
WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "receive_generated_deck"
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


@app.task(bind=True)
def generate_deck_task(self, sid, data_json):
    data = json.loads(data_json)
    session_id = data["session_id"]
    system_message_id = data["system_message_id"]
    user_id = data["user_id"]
    organisation_id = data["organisation_id"]
    logger.info(
        f"Celery task generate_deck_task started: sid={sid}, session_id={session_id}, message_id={system_message_id}"
    )

    master_session = next(get_master_session())
    try:
        statement = select(ChatSessionDB).where(
            ChatSessionDB.session_id == session_id,
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted == False,
        )
        chat = master_session.exec(statement).one_or_none()
        logger.info(
            f"Fetched chat session: session_id={session_id}, user_id={user_id}, organisation_id={organisation_id}"
        )
        if not chat:
            raise RuntimeError("Chat session not found")

        deck_message = master_session.exec(
            select(ChatMessageDB).where(
                (ChatMessageDB.session_id == session_id)
                & (ChatMessageDB.message_id == system_message_id)
            )
        ).first()
        if not deck_message:
            raise RuntimeError("Chat message not found")

        agent = DeckGeneration(provider="azure", model_name="o3-mini")
        logger.info(
            f"Initialized deck generation workflow for session_id={session_id}, message_id={system_message_id}"
        )

        async def _run_workflow():
            full_response = ""
            async for chunk in agent.run_workflow(deck_message):
                is_complete = chunk.status == "COMPLETED"
                if chunk.content:
                    full_response = full_response + chunk.content.get("public_url", "")
                    response_data = {
                        "chat_id": session_id,
                        "message_id": system_message_id,
                        "isComplete": is_complete,
                        "isLineComplete": True,
                        "isError": False,
                        "response": chunk.content.get("public_url", ""),
                        "message_type": "TECHNICAL_DECK",
                        "extras": {
                            "completion_percentage": chunk.content.get(
                                "completion_percentage", 0
                            ),
                        },
                    }
                    publish_websocket_message(sid, RESPONSE_EVENT, response_data)
            return full_response

        full_response = asyncio.run(_run_workflow())
        logger.info(
            f"Deck generation workflow completed, total chunks={len(full_response)}"
        )

        deck_message.technical_deck_details = {
            **deck_message.technical_deck_details,
            "technical_deck_url": full_response,
            "state": "SLIDE_OUTPUT",
        }
        master_session.add(deck_message)
        master_session.commit()
        logger.info(f"Deck message updated in database - {deck_message}")

    except Exception as e:
        logger.exception("Error in Celery generate_deck_task")
        error_data = {
            "chat_id": session_id,
            "message_id": system_message_id,
            "isComplete": True,
            "isLineComplete": True,
            "isError": True,
            "errorResponse": str(e),
            "message_type": "TECHNICAL_DECK",
        }
        publish_websocket_message(sid, RESPONSE_EVENT, error_data)
    finally:
        master_session.close()
