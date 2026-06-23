"""Present CRM activity-feed service records as API schemas."""

from __future__ import annotations

from uuid import UUID

from src.api.schemas.crm_activity_feed_models import ActivityFeedItem, ActivityFeedPage
from src.service.crm.activity_feed_read_service import ActivityFeedRecord


def present_activity_feed_item(record: ActivityFeedRecord) -> ActivityFeedItem:
    """Map a joined activity/link record into an API feed card."""
    activity = record.activity
    link = record.link
    return ActivityFeedItem(
        activity_id=activity.activity_id,
        activity_type=activity.activity_type,
        source_type=activity.source_type,
        source_record_id=activity.source_record_id,
        source_provider=activity.source_provider,
        source_integration_id=activity.source_integration_id,
        source_url=activity.source_url,
        title=activity.title,
        summary=activity.summary,
        preview=activity.preview,
        occurred_at=link.occurred_at,
        actor_name=activity.actor_name,
        actor_email=activity.actor_email,
        display_payload=activity.display_payload or {},
        link_role=link.link_role,
        link_source=link.link_source,
        confidence_score=float(link.confidence_score)
        if link.confidence_score is not None
        else None,
        created_at=activity.created_at,
        updated_at=activity.updated_at,
    )


def present_activity_feed_page(
    *,
    object_type: str,
    object_id: UUID,
    records: list[ActivityFeedRecord],
    total_count: int,
    cursor: str | None,
) -> ActivityFeedPage:
    """Map activity-feed records into a paginated API response."""
    return ActivityFeedPage(
        object_type=object_type,
        object_id=object_id,
        items=[present_activity_feed_item(record) for record in records],
        total_count=total_count,
        cursor=cursor,
    )
