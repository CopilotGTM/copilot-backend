from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.infra.utils.logger import get_logger

logger = get_logger("kw_progress")


def publish(
    organisation_id: str,
    source_id: Optional[str],
    *,
    status: str,
    progress: Optional[int] = None,
    updated_at: Optional[str] = None,
    error_message: Optional[str] = None,
    thumbnail_small_url: Optional[str] = None,
    thumbnail_medium_url: Optional[str] = None,
) -> None:
    if not source_id:
        return
    try:
        event = KnowledgeSourceEventUpdate(
            source_id=str(source_id),
            status=status,
            updated_at=updated_at or datetime.utcnow().isoformat(),
            error_message=error_message,
            progress=progress,
            thumbnail_small_url=thumbnail_small_url,
            thumbnail_medium_url=thumbnail_medium_url,
        )
        publish_source_status(organisation_id, event)
    except Exception as e:  # pragma: no cover - non-critical telemetry
        logger.debug(f"Failed to publish progress for source {source_id}: {e}")
