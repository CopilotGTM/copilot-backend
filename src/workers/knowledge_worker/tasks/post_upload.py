"""Post-upload processing task for knowledge sources.

This task delegates all business logic to PostUploadService.
The task file contains only the Celery task definition.
"""

from __future__ import annotations

from uuid import UUID

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.services import PostUploadService

logger = get_logger("knowledge_post_upload_task")


@app.task(name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3)
def post_upload_process(self, organisation_id: UUID, source_id: UUID):  # noqa: ARG001
    """Post-upload processing: checksum, thumbnails, and trigger extraction.

    Delegates to PostUploadService which handles:
    - Loading source data
    - Computing checksums (Azure blob operation)
    - Generating thumbnails (Azure blob operation)
    - Duplicate detection
    - Updating status
    - Triggering document extraction

    The service follows the fresh session pattern internally.
    """
    logger.info("post_upload_process started org=%s source=%s", organisation_id, source_id)

    service = PostUploadService(organisation_id)
    success = service.process(source_id)

    if success:
        logger.info("post_upload_process completed source=%s", source_id)
    else:
        logger.warning("post_upload_process failed source=%s", source_id)
