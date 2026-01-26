"""Post-upload processing task for chat attachments.

This task delegates all business logic to PostUploadService.
The task file contains only the Celery task definition.
"""

from __future__ import annotations

from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services import PostUploadService

logger = get_logger("attachments_post_upload_task")


@app.task(name="attachments.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, attachment_id: str):  # noqa: ARG001
    """Post-upload processing: checksum and thumbnail generation.

    Delegates to PostUploadService which handles:
    - Loading attachment data
    - Computing checksums (Azure blob operation)
    - Generating thumbnails (Azure blob operation)
    - Updating status

    The service follows the fresh session pattern internally.
    """
    logger.info(
        "Attachment post-upload task started org=%s attachment=%s",
        organisation_id,
        attachment_id,
    )

    service = PostUploadService(organisation_id)
    success = service.process(attachment_id)

    if success:
        logger.info("Attachment post-upload task completed attachment=%s", attachment_id)
    else:
        logger.warning("Attachment post-upload task failed attachment=%s", attachment_id)
