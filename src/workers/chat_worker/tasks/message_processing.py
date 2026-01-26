"""Message processing task for chat worker.

This task delegates all business logic to MessageProcessingService.
The task file contains only the Celery task definition.
"""

from __future__ import annotations

from uuid import UUID

from celery.exceptions import Retry

from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services import MessageProcessingService

logger = get_logger("chat_message_processing_task")


@app.task(name="chat.process_message", acks_late=True, bind=True, max_retries=5)
def process_message(self, organisation_id: UUID, job_id: UUID):
    """Process a chat message job.

    Delegates to MessageProcessingService which handles:
    - Loading job and conversation data
    - Executing agent workflows (no session held during LLM calls)
    - Saving results and finalizing jobs

    The service follows the fresh session pattern internally.
    """
    logger.info("Processing chat message job org=%s job=%s", organisation_id, job_id)

    try:
        service = MessageProcessingService(organisation_id)
        success = service.process(job_id, celery_task=self)

        if success:
            logger.info("Chat message job completed job=%s", job_id)
        else:
            logger.warning("Chat message job failed job=%s", job_id)

    except Retry:
        # Re-raise Celery retry exceptions
        raise
    except Exception as e:
        logger.exception("Chat message job failed with exception job=%s error=%s", job_id, e)
        raise
