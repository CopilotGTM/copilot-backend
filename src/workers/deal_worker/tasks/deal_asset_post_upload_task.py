"""Run and publish committed deal-asset post-upload state changes."""

from __future__ import annotations

from src.infra.messaging.deal_asset_status_publish import publish_deal_asset_status
from src.infra.utils.logger import get_logger
from src.service.crm.assets.post_upload_service import (
    DealAssetPostUploadService,
    PostUploadStatus,
)
from src.workers.deal_worker.celery_app import app

logger = get_logger("deal_asset_post_upload_task")


@app.task(name="deal_asset.post_upload", bind=True, acks_late=True, max_retries=3)
def deal_asset_post_upload(self, organisation_id: str, asset_id: str) -> dict[str, str]:
    """Finalize one asset, publish its committed state, and retry transient work."""
    logger.info(
        "Deal asset post-upload task started",
        extra={
            "event": "deal_asset_post_upload_task_started",
            "organisation_id": organisation_id,
            "asset_id": asset_id,
        },
    )

    try:
        outcome = DealAssetPostUploadService(organisation_id).process(asset_id)
        if outcome.event is not None:
            # The service has committed before returning, so publication can be
            # retried without exposing an uncommitted asset state.
            publish_deal_asset_status(organisation_id, outcome.event)
    except Exception as exc:
        _retry_or_raise(self, asset_id, exc)

    if outcome.status is PostUploadStatus.RETRYABLE_FAILURE:
        _retry_or_raise(
            self,
            asset_id,
            RuntimeError(outcome.error or "Deal asset post-upload failed"),
        )

    if outcome.status in {
        PostUploadStatus.PROCESSED,
        PostUploadStatus.ALREADY_PROCESSED,
    }:
        logger.info(
            "Deal asset post-upload completed",
            extra={
                "event": "deal_asset_post_upload_task_completed",
                "organisation_id": organisation_id,
                "asset_id": asset_id,
            },
        )
    elif outcome.status is PostUploadStatus.INVALID:
        logger.warning(
            "Deal asset post-upload rejected invalid work",
            extra={
                "event": "deal_asset_post_upload_task_invalid",
                "organisation_id": organisation_id,
                "asset_id": asset_id,
                "error": outcome.error,
            },
        )

    return {"status": outcome.status.value, "asset_id": asset_id}


def _retry_or_raise(self, asset_id: str, exc: Exception) -> None:
    """Retry transient post-upload work, then expose permanent exhaustion."""
    if self.request.retries >= self.max_retries:
        raise RuntimeError(f"Deal asset post-upload exhausted retries for {asset_id}") from exc

    countdown = 60 * (2**self.request.retries)
    raise self.retry(exc=exc, countdown=countdown)
