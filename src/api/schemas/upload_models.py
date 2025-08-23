from __future__ import annotations

from uuid import UUID
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator
from datetime import datetime

from src.infra.config.configs import chat_file_upload_config
from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus

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
    """Minimal presign response for chat attachments.

    Harmonised with batch variant so clients can treat single/batch uniformly.
    (Added success & error_message fields for parity; error_message always None on success.)
    """
    attachment_id: UUID
    filename: str
    mimetype: str
    upload_url: HttpUrl
    size_bytes: int = Field(..., description="Client-reported size of the file in bytes")
    expires_in_seconds: int = Field(..., description="Seconds until the upload URL expires")
    status: AttachmentStatus = Field(..., description="Logical status for the attachment record (e.g. pending_upload)")
    success: bool = Field(True, description="Indicates whether the presign operation succeeded (always true for this model)")
    error_message: str | None = Field(None, description="Present only when success is false (kept for shape parity with batch)")


class FinalizeRequest(BaseModel):
    """Request to finalize an upload after the file is in storage."""

    attachment_id: UUID = Field(..., description="The ID of the attachment to finalize.")
    checksum: Optional[str] = Field(
        default=None,
        description="The MD5 or SHA256 checksum of the uploaded file for integrity verification (optional)."
    )

class UploadedAttachmentResponse(BaseModel):
    """Response model representing a successfully uploaded and finalized attachment."""
    # Minimal finalized attachment payload. Keep this small for chat clients.
    attachment_id: UUID
    uploaded_at: datetime
    status: AttachmentStatus = Field(..., description="Logical status for the attachment record (e.g. processed|failed)")

    # Keep model compatibility for object creation from DB models
    model_config = {"from_attributes": True}


class AttachmentDownloadResponse(BaseModel):
    """Response model for generating a short‑lived download URL for an attachment."""
    attachment_id: UUID
    download_url: HttpUrl
    expires_in_seconds: int = Field(default=120 * 60, description="Seconds until the URL expires")


# ---------------------------- Batch Models ---------------------------- #

class AttachmentBatchPresignRequest(BaseModel):
    """Batch request to presign one or more attachments.

    Accepts a list of individual PresignRequest objects. Validation for each
    file (size/mimetype) is handled by PresignRequest's validators.
    """
    files: list[PresignRequest] = Field(..., min_length=1, description="List of files to presign (1..N)")


class AttachmentPresignFileResponse(BaseModel):
    """Per-file presign result (success or failure).

    Status now uses AttachmentStatus enum for consistency with single presign.
    """
    attachment_id: UUID | None = Field(None, description="Attachment ID (None if creation failed early)")
    filename: str
    mimetype: str | None = None
    upload_url: HttpUrl | None = None
    size_bytes: int | None = Field(None, description="Client-reported size of the file in bytes (None if failed)")
    success: bool = True
    error_message: str | None = None
    expires_in_seconds: int = Field(0, description="Seconds until the upload URL expires (0 if failed)")
    status: AttachmentStatus | None = Field(None, description="Logical status for the attachment record (e.g. pending_upload|failed)")


class AttachmentBatchPresignResponse(BaseModel):
    """Envelope aggregating per-file responses plus raw error objects for diagnostics."""
    files: list[AttachmentPresignFileResponse]
    errors: list[dict] = Field(default_factory=list)


class AttachmentBatchFinalizeRequest(BaseModel):
    """Batch finalize request for one or more attachments."""
    files: list[FinalizeRequest] = Field(..., min_length=1)


class AttachmentBatchFinalizeResult(BaseModel):
    attachment_id: UUID
    status: str | None = None
    error: str | None = None
    updated_at: datetime | None


class AttachmentBatchFinalizeResponse(BaseModel):
    results: list[AttachmentBatchFinalizeResult]
    errors: list[dict] = Field(default_factory=list)
