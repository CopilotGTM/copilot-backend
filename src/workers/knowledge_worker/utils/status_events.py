from __future__ import annotations

from typing import Optional

from src.infra.messaging.knowledge_source_status_publish import (
    publish_source_status,
)
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate


def _status_to_str(status_enum) -> Optional[str]:
    if status_enum is None:
        return None
    return getattr(status_enum, "value", str(status_enum))


def publish_knowledge_source_status(
    organisation_id: str,
    *,
    source,
    status_override=None,
    progress: Optional[int] = None,
    thumbnail_small_url: Optional[str] = None,
    thumbnail_medium_url: Optional[str] = None,
) -> None:
    """Publish a knowledge source status update for various task stages.

    - Accepts either Enum or string for status via `status_override`; defaults to `source.status`.
    - Allows callers to optionally pass thumbnail URLs and progress (0-100).
    """
    status_val = (
        status_override
        if status_override is not None
        else getattr(source, "status", None)
    )

    event = KnowledgeSourceEventUpdate(
        source_id=str(source.source_id),
        status=_status_to_str(status_val),
        updated_at=(
            source.updated_at.isoformat()
            if getattr(source, "updated_at", None)
            else None
        ),
        error_message=getattr(source, "error_message", None),
        progress=progress,
        thumbnail_medium_url=thumbnail_medium_url,
        thumbnail_small_url=thumbnail_small_url,
    )
    publish_source_status(organisation_id, event)
