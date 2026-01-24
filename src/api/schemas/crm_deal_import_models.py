from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DealImportFileType(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"


class DealImportTemplateResponse(BaseModel):
    file_type: DealImportFileType
    download_url: str
    expires_in_seconds: int
    storage_path: str | None = None


class DealImportUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    file_type: DealImportFileType | None = None

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("filename cannot be empty")
        return value


class DealImportUploadResponse(BaseModel):
    upload_url: str
    storage_path: str
    expires_in_seconds: int


class DealImportProcessRequest(BaseModel):
    storage_path: str = Field(..., min_length=1)
    file_type: DealImportFileType | None = None


class DealImportProcessResponse(BaseModel):
    job_id: UUID
    job_type: str
    status: str
    stream_url: str | None = None
