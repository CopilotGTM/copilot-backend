from __future__ import annotations

from uuid import UUID
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, HttpUrl, field_validator
from datetime import datetime

from src.infra.config.configs import knowledge_file_upload_config

# Knowledge base specific constants (can later be moved to its own config class)
KNOWLEDGE_MAX_FILE_SIZE_BYTES = knowledge_file_upload_config.MAX_FILE_SIZE_BYTES
KNOWLEDGE_ALLOWED_MIMETYPES = set(knowledge_file_upload_config.ALLOWED_MIMETYPES)
MAX_BATCH_FILES = 50  # hard safety cap


class KnowledgePresignFileRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    mimetype: str = Field(...)
    size_bytes: int = Field(..., gt=0)

    @field_validator("mimetype")
    def validate_mimetype(cls, v: str) -> str:
        if v not in KNOWLEDGE_ALLOWED_MIMETYPES:
            raise ValueError(f"MIME type '{v}' is not allowed for knowledge uploads.")
        return v

    @field_validator("size_bytes")
    def validate_size(cls, v: int) -> int:
        if v > KNOWLEDGE_MAX_FILE_SIZE_BYTES:
            raise ValueError(
                f"File size of {v} bytes exceeds limit of {KNOWLEDGE_MAX_FILE_SIZE_BYTES} bytes."
            )
        return v


class KnowledgeBatchPresignRequest(BaseModel):
    files: List[KnowledgePresignFileRequest] = Field(..., min_length=1)
    default_filters: Dict[str, Any] | None = Field(
        default=None, description="Optional default filters/tags applied to each source"
    )
    client_batch_id: Optional[str] = Field(
        default=None,
        description="Optional client-generated idempotency key to detect duplicate batch submissions.",
        max_length=100,
    )

    @field_validator("files")
    def validate_batch_len(cls, v: List[KnowledgePresignFileRequest]):
        if len(v) > MAX_BATCH_FILES:
            raise ValueError(f"Too many files in one batch. Max allowed is {MAX_BATCH_FILES}.")
        return v


class KnowledgePresignFileResponse(BaseModel):
    source_id: UUID
    filename: str
    mimetype: str
    size_bytes: int
    upload_url: Optional[HttpUrl] = None
    blob_path: str = ""
    expires_in_seconds: int = 0
    status: str
    error_message: Optional[str] = None
    success: bool = True


class KnowledgeBatchPresignResponse(BaseModel):
    files: List[KnowledgePresignFileResponse]
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class KnowledgeFinalizeRequest(BaseModel):
    source_id: UUID
    checksum: Optional[str] = Field(
        default=None,
        description="Optional checksum (MD5/SHA256). Stored for reference; actual calc may occur async.",
    )


class KnowledgeBatchFinalizeRequest(BaseModel):
    files: List[KnowledgeFinalizeRequest] = Field(..., min_length=1)


class KnowledgeSourceResponse(BaseModel):
    source_id: UUID
    filename: Optional[str]
    mimetype: Optional[str]
    size_bytes: Optional[int]
    status: str
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None
    storage_path: Optional[str] = None
    read_url: Optional[HttpUrl] = None
    filters: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"from_attributes": True}
