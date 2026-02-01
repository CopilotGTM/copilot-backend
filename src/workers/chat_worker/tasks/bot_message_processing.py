"""Bot message processing task for chat worker.

Processes messages from bot platforms (Teams, Slack) through the GeneralAgent
and sends the reply back to the originating platform.

This task delegates all business logic to BotMessageProcessingService.
"""

from __future__ import annotations

from uuid import UUID

from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services import BotMessageProcessingService

logger = get_logger("bot_message_processing_task")


@app.task(name="chat.process_bot_message", acks_late=True, bind=True, max_retries=3)
def process_bot_message(
    self,
    organisation_id: str,
    user_id: str,
    message_text: str,
    provider: str,
    reply_context: dict,
    chat_history: list[dict] | None = None,
    bot_context: dict | None = None,
    user_email: str | None = None,
):
    """Process a bot message through GeneralAgent and send reply.

    Args:
        organisation_id: The organisation UUID (string)
        user_id: The linked CopilotGTM user UUID (string)
        message_text: The user's message text
        provider: Bot platform identifier (e.g. "teams")
        reply_context: Platform-specific context for sending the reply
        chat_history: Optional previous conversation messages
        bot_context: Optional context for bot-specific features (e.g. group chat metadata)
        user_email: The email of the user who sent the message
    """
    logger.info(
        "Processing bot message org=%s user=%s provider=%s bot_context=%s",
        organisation_id,
        user_id,
        provider,
        bool(bot_context),
    )

    try:
        service = BotMessageProcessingService(UUID(organisation_id))
        success = service.process(
            user_id=UUID(user_id),
            message_text=message_text,
            provider=provider,
            reply_context=reply_context,
            chat_history=chat_history,
            bot_context=bot_context,
            user_email=user_email,
        )

        if success:
            logger.info(
                "Bot message processed successfully org=%s provider=%s", organisation_id, provider
            )
        else:
            logger.warning(
                "Bot message processing failed org=%s provider=%s", organisation_id, provider
            )

    except Exception as e:
        logger.exception("Bot message task failed org=%s error=%s", organisation_id, e)
        raise
