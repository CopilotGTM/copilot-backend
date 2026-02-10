from __future__ import annotations

import re
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DealImportFileType(str, Enum):
    CSV = "csv"


class DealImportMode(str, Enum):
    CREATE_ONLY = "create_only"
    UPDATE_ONLY = "update_only"
    UPSERT = "upsert"


class DealImportCustomColumnType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    INTEGER = "integer"
    DATE = "date"
    DATETIME = "datetime"
    CURRENCY = "currency"
    BOOLEAN = "boolean"


_CUSTOM_KEY_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


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
        if not value.lower().endswith(".csv"):
            raise ValueError("Only CSV files are supported")
        return value


class DealImportUploadResponse(BaseModel):
    upload_url: str
    storage_path: str
    expires_in_seconds: int


class DealImportProcessRequest(BaseModel):
    storage_path: str = Field(..., min_length=1)
    file_type: DealImportFileType | None = None
    import_mode: DealImportMode = DealImportMode.UPSERT
    emit_row_events: bool = True
    strict_validation: bool = False
    custom_column_types: dict[str, DealImportCustomColumnType] | None = None
    custom_column_labels: dict[str, str] | None = None

    @field_validator("custom_column_types", mode="before")
    @classmethod
    def normalize_custom_column_types(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("custom_column_types must be an object")

        normalized: dict[str, object] = {}
        for raw_key, raw_type in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("custom_column_types keys must be strings")
            key = raw_key.strip().lower()
            key = key.replace("-", "_").replace(" ", "_")
            key = _CUSTOM_KEY_SANITIZE_RE.sub("_", key)
            key = "_".join(part for part in key.split("_") if part)
            if key.startswith("custom_"):
                key = key[len("custom_") :]
            if not key:
                raise ValueError("custom_column_types contains an empty key")
            normalized[key] = raw_type
        return normalized

    @field_validator("custom_column_labels", mode="before")
    @classmethod
    def normalize_custom_column_labels(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("custom_column_labels must be an object")

        normalized: dict[str, str] = {}
        for raw_key, raw_label in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("custom_column_labels keys must be strings")
            if raw_label is None:
                continue
            label = str(raw_label).strip()
            if not label:
                continue
            key = raw_key.strip().lower()
            key = key.replace("-", "_").replace(" ", "_")
            key = _CUSTOM_KEY_SANITIZE_RE.sub("_", key)
            key = "_".join(part for part in key.split("_") if part)
            if key.startswith("custom_"):
                key = key[len("custom_") :]
            if key:
                normalized[key] = label
        return normalized or None


class DealImportProcessResponse(BaseModel):
    job_id: UUID | None = None
    job_type: str | None = None
    status: str
    stream_url: str | None = None
    requires_custom_column_mapping: bool = False
    unknown_custom_columns: list[str] | None = None
    suggested_types: dict[str, DealImportCustomColumnType] | None = None
