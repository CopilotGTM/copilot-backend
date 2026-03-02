"""CRM API response models for deal memory."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DealMemoryResponse(BaseModel):
    """Deal memory document returned to the frontend."""

    memory_id: UUID = Field(description="Memory record UUID")
    deal_id: UUID = Field(description="Deal this memory belongs to")
    content: str = Field(description="Markdown memory document")
    version: int = Field(description="Document version (increments on each update)")
    token_count: int | None = Field(default=None, description="Approximate token count")
    created_at: datetime = Field(description="When the memory was first created")
    updated_at: datetime = Field(description="When the memory was last updated")

    model_config = {"from_attributes": True}
