from __future__ import annotations

from datetime import UTC, datetime

from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.infra.utils.logger import get_logger

logger = get_logger("kw_progress")


class ProgressPublisher:
    """Lightweight progress event publisher used across the knowledge worker.

    - Per-run instances: initialize with `organisation_id` and `source_id` and
      reuse the same instance for all progress updates in that run.
    - A legacy module-level `publish` function remains for one-off calls and
      delegates to a temporary instance.
    """

    def __init__(
        self,
        organisation_id: str | None = None,
        source_id: str | None = None,
    ) -> None:
        self.organisation_id = organisation_id
        self.source_id = source_id

    def publish(
        self,
        status: str,
        progress: int | None = None,
        error_message: str | None = None,
        thumbnail_small_url: str | None = None,
        thumbnail_medium_url: str | None = None,
    ) -> None:
        org = self.organisation_id
        src = self.source_id
        if not src or not org:
            return
        try:
            event = KnowledgeSourceEventUpdate(
                source_id=str(src),
                status=status,
                updated_at=datetime.now(UTC).isoformat(),
                error_message=error_message,
                progress=progress,
                thumbnail_small_url=thumbnail_small_url,
                thumbnail_medium_url=thumbnail_medium_url,
            )
            publish_source_status(org, event)
        except Exception as e:  # pragma: no cover - non-critical telemetry
            logger.debug(f"Failed to publish progress for source {src}: {e}")
