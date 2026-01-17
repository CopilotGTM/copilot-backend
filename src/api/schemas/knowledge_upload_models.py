from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator
from src.infra.config.configs import knowledge_file_upload_config

# Re-export event model from infra for backwards compatibility
from src.infra.messaging.event_models import (
    KnowledgeSourceEventUpdate as KnowledgeSourceEventUpdate,
)

# Knowledge base specific constants (can later be moved to its own config class)
KNOWLEDGE_MAX_FILE_SIZE_BYTES = knowledge_file_upload_config.MAX_FILE_SIZE_BYTES
KNOWLEDGE_ALLOWED_MIMETYPES = set(knowledge_file_upload_config.ALLOWED_MIMETYPES)
MAX_BATCH_FILES = 50  # hard safety cap


class KnowledgePresignFileRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    mimetype: str = Field(...)
    size_bytes: int = Field(..., gt=0)

    @field_validator("mimetype")
    @classmethod
    def validate_mimetype(cls, v: str) -> str:
        if v not in KNOWLEDGE_ALLOWED_MIMETYPES:
            raise ValueError(f"MIME type '{v}' is not allowed for knowledge uploads.")
        return v

    @field_validator("size_bytes")
    @classmethod
    def validate_size(cls, v: int) -> int:
        if v > KNOWLEDGE_MAX_FILE_SIZE_BYTES:
            raise ValueError(
                f"File size of {v} bytes exceeds limit of {KNOWLEDGE_MAX_FILE_SIZE_BYTES} bytes."
            )
        return v


class KnowledgeBatchPresignRequest(BaseModel):
    files: list[KnowledgePresignFileRequest] = Field(..., min_length=1)
    default_filters: dict[str, Any] | None = Field(
        default=None, description="Optional default filters/tags applied to each source"
    )
    client_batch_id: str | None = Field(
        default=None,
        description="Optional client-generated idempotency key to detect duplicate batch submissions.",
        max_length=100,
    )

    @field_validator("files")
    @classmethod
    def validate_batch_len(cls, v: list[KnowledgePresignFileRequest]):
        if len(v) > MAX_BATCH_FILES:
            raise ValueError(f"Too many files in one batch. Max allowed is {MAX_BATCH_FILES}.")
        return v


class KnowledgePresignFileResponse(BaseModel):
    source_id: UUID
    filename: str
    mimetype: str
    size_bytes: int
    upload_url: HttpUrl | None = None
    expires_in_seconds: int = 0
    status: str
    error_message: str | None = None
    success: bool = True


class KnowledgeBatchPresignResponse(BaseModel):
    files: list[KnowledgePresignFileResponse]
    errors: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeFinalizeRequest(BaseModel):
    source_id: UUID
    checksum: str | None = Field(
        default=None,
        description="Optional checksum (MD5/SHA256). Stored for reference; actual calc may occur async.",
    )


class KnowledgeBatchFinalizeRequest(BaseModel):
    files: list[KnowledgeFinalizeRequest] = Field(..., min_length=1)


class KnowledgeSourceResponse(BaseModel):
    source_id: UUID
    filename: str | None
    mimetype: str | None
    size_bytes: int | None
    status: str
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None
    storage_path: str | None = None
    read_url: HttpUrl | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    checksum: str | None = None
    thumbnail_small_path: str | None = None
    thumbnail_medium_path: str | None = None

    model_config = {"from_attributes": True}
