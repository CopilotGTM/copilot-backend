"""SSE event models for worker status updates.

These models are used by workers to publish status updates via SSE.
They are defined here in infra to avoid workers depending on the API layer.
"""

from __future__ import annotations

from pydantic import BaseModel


class KnowledgeSourceEventUpdate(BaseModel):
    """SSE event model for knowledge source status updates."""

    source_id: str
    status: str
    updated_at: str | None = None
    error_message: str | None = None
    progress: int | None = None
    thumbnail_small_url: str | None = None
    thumbnail_medium_url: str | None = None

    model_config = {
        "from_attributes": True,
        "extra": "allow",
    }


class AttachmentEventUpdate(BaseModel):
    """SSE event model for attachment status updates."""

    attachment_id: str
    status: str
    updated_at: str | None = None
    error_message: str | None = None
    thumbnail_small_url: str | None = None

    model_config = {"from_attributes": True}


__all__ = [
    "KnowledgeSourceEventUpdate",
    "AttachmentEventUpdate",
]
