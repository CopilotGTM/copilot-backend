"""Present CRM activity-feed service records as API schemas."""

from __future__ import annotations

from uuid import UUID

from src.api.schemas.crm_activity_feed_models import (
    ActivityFeedItem,
    ActivityFeedPage,
    ActivityFeedRelatedObject,
)
from src.service.crm.activity_feed_read_service import ActivityFeedRecord


def present_activity_feed_item(record: ActivityFeedRecord) -> ActivityFeedItem:
    """Map a joined activity/link record into an API feed card."""
    activity = record.activity
    link = record.link
    related_targets: dict[tuple[str, UUID], set[str]] = {}
    for related_link in record.related_links:
        key = (related_link.object_type, related_link.object_id)
        related_targets.setdefault(key, set()).add(related_link.link_role)
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
        occurred_at=record.occurred_at or link.occurred_at,
        actor_name=activity.actor_name,
        actor_email=activity.actor_email,
        display_payload=activity.display_payload or {},
        link_role=link.link_role,
        link_source=link.link_source,
        confidence_score=float(link.confidence_score)
        if link.confidence_score is not None
        else None,
        related_objects=[
            ActivityFeedRelatedObject(
                object_type=object_type,
                object_id=object_id,
                link_roles=sorted(link_roles),
            )
            for (object_type, object_id), link_roles in related_targets.items()
        ],
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
