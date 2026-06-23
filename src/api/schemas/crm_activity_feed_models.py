"""CRM activity feed API response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field


class ActivityFeedQueryParams(BaseModel):
    """Query parameters for CRM object activity-feed listing."""

    limit: int = Query(20, ge=1, le=100, description="Number of activities per page")
    cursor: str | None = Query(None, description="Pagination cursor")
    activity_types: str | None = Query(
        None,
        description="Comma-separated activity types to include, for example NOTE,TASK,EMAIL",
    )
    source_types: str | None = Query(
        None,
        description="Comma-separated source kinds to include, for example NOTE,TASK,MEETING",
    )
    start_date: datetime | None = Query(None, description="Filter activities after this date")
    end_date: datetime | None = Query(None, description="Filter activities before this date")
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order")


class ActivityFeedItem(BaseModel):
    """One activity-feed card backed by crm_activity_links and crm_activities."""

    activity_id: UUID
    activity_type: str | None = None
    source_type: str | None = None
    source_record_id: str | None = None
    source_provider: str | None = None
    source_integration_id: UUID | None = None
    source_url: str | None = None
    title: str | None = None
    summary: str | None = None
    preview: str | None = None
    occurred_at: datetime
    actor_name: str | None = None
    actor_email: str | None = None
    display_payload: dict[str, Any] = Field(default_factory=dict)
    link_role: str
    link_source: str
    confidence_score: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActivityFeedPage(BaseModel):
    """Paginated CRM object activity feed."""

    object_type: str
    object_id: UUID
    items: list[ActivityFeedItem]
    total_count: int
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")
