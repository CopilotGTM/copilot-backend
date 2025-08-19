from __future__ import annotations

from uuid import UUID
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator
from datetime import datetime

from src.infra.config.configs import chat_file_upload_config

# Centralized constants for validation, sourced from the application config.
# This makes the API schema the single source of truth for request validation.
MAX_FILE_SIZE_BYTES = chat_file_upload_config.MAX_FILE_SIZE_BYTES
ALLOWED_MIMETYPES = set(chat_file_upload_config.ALLOWED_MIMETYPES)


class PresignRequest(BaseModel):
    """Request to generate a presigned URL for file upload."""

    filename: str = Field(..., min_length=1, max_length=255, description="The original filename of the attachment.")
    mimetype: str = Field(..., description="The MIME type of the file.")
    size_bytes: int = Field(..., gt=0, description="The size of the file in bytes.")

    @field_validator("mimetype")
    def validate_mimetype(cls, v: str) -> str:
        if v not in ALLOWED_MIMETYPES:
            raise ValueError(f"MIME type '{v}' is not allowed.")
        return v

    @field_validator("size_bytes")
    def validate_size(cls, v: int) -> int:
        if v > MAX_FILE_SIZE_BYTES:
            raise ValueError(f"File size of {v} bytes exceeds the limit of {MAX_FILE_SIZE_BYTES} bytes.")
        return v


class PresignResponse(BaseModel):
    """Response containing the presigned URL and attachment details."""

    attachment_id: UUID = Field(..., description="The unique ID for the attachment record.")
    upload_url: HttpUrl = Field(..., description="The presigned URL to use for the PUT request.")
    blob_path: str = Field(..., description="The storage path of the blob.")
    expires_in_seconds: int = Field(..., description="The number of seconds the upload_url is valid for.")


class FinalizeRequest(BaseModel):
    """Request to finalize an upload after the file is in storage."""

    attachment_id: UUID = Field(..., description="The ID of the attachment to finalize.")
    checksum: str = Field(..., description="The MD5 or SHA256 checksum of the uploaded file for integrity verification.")


class UploadedAttachmentResponse(BaseModel):
    """Response model representing a successfully uploaded and finalized attachment."""

    attachment_id: UUID
    filename: str
    url: HttpUrl
    mimetype: str
    size_bytes: int
    uploaded_at: datetime
    checksum: Optional[str] = None
    virus_scan_status: Optional[str] = "pending"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    processing_status: Optional[str] = Field(
        default="pending", description="Processing pipeline status"
    )
    thumbnail_small_url: Optional[HttpUrl] = Field(
        default=None, description="Temporary SAS URL for the small thumbnail if generated"
    )
    thumbnail_medium_url: Optional[HttpUrl] = Field(
        default=None, description="Temporary SAS URL for the medium thumbnail if generated"
    )

    model_config = {
        "from_attributes": True
    }
