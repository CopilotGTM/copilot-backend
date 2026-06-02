"""Pydantic schemas for Reminder API endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field
from src.contracts.reminders import (
    VALID_DELIVERY_CHANNELS,
    AgentCreateRequest,
    AgentUpdateRequest,
    ReminderCreateRequest,
    ReminderDetail,
    ReminderHistoryResponse,
    ReminderListItem,
    ReminderListResponse,
    ReminderRunListItem,
    ReminderUpdateRequest,
    StaticReminderCreateRequest,
    StaticReminderUpdateRequest,
)


class ReminderListQueryParams(BaseModel):
    """Query parameters for listing reminders."""

    limit: int = Query(20, ge=1, le=100, description="Items per page")
    page: int = Query(1, ge=1, description="Page number (1-indexed)")
    status: str | None = Query(
        None, description="Comma-separated status filter (active,paused,completed)"
    )
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort by next_trigger_at")


class ReminderHistoryQueryParams(BaseModel):
    """Query parameters for reminder execution history."""

    limit: int = Query(20, ge=1, le=100, description="Items per page")
    page: int = Query(1, ge=1, description="Page number (1-indexed)")


class DeliveryChannelInfo(BaseModel):
    """Information about a single delivery channel."""

    channel: str = Field(description="Channel identifier: 'teams' or 'slack'")
    available: bool = Field(description="Whether the user has this channel set up and ready to use")
    label: str = Field(description="Human-readable label for the channel")
    mapping_id: UUID | None = Field(
        default=None,
        description="Identity mapping ID to send back when creating a reminder with this channel.",
    )


class AvailableChannelsResponse(BaseModel):
    """Response listing delivery channels and their availability for the current user."""

    channels: list[DeliveryChannelInfo]


__all__ = [
    "AgentCreateRequest",
    "AgentUpdateRequest",
    "AvailableChannelsResponse",
    "DeliveryChannelInfo",
    "ReminderCreateRequest",
    "ReminderDetail",
    "ReminderHistoryQueryParams",
    "ReminderHistoryResponse",
    "ReminderListItem",
    "ReminderListQueryParams",
    "ReminderListResponse",
    "ReminderRunListItem",
    "ReminderUpdateRequest",
    "StaticReminderCreateRequest",
    "StaticReminderUpdateRequest",
    "VALID_DELIVERY_CHANNELS",
]
