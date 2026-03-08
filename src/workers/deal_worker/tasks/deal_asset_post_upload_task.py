"""Post-upload processing task for deal assets.

Delegates all business logic to DealAssetPostUploadService.
The task file contains only the Celery task definition.
"""

from __future__ import annotations

from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_asset_post_upload_service import (
    DealAssetPostUploadService,
)

logger = get_logger("deal_asset_post_upload_task")


@app.task(name="deal_asset.post_upload", bind=True, acks_late=True, max_retries=3)
def deal_asset_post_upload(self, organisation_id: str, asset_id: str):  # noqa: ARG001
    """Post-upload processing: checksum and thumbnail generation for deal assets.

    Delegates to DealAssetPostUploadService which handles:
    - Loading asset data
    - Computing checksums (Azure blob operation)
    - Generating thumbnails (Azure blob operation)
    - Updating status + publishing SSE event

    The service follows the fresh session pattern internally.
    """
    logger.info(
        "Deal asset post-upload task started org=%s asset=%s",
        organisation_id,
        asset_id,
    )

    service = DealAssetPostUploadService(organisation_id)
    success = service.process(asset_id)

    if success:
        logger.info("Deal asset post-upload completed asset=%s", asset_id)
    else:
        logger.warning("Deal asset post-upload failed asset=%s", asset_id)
