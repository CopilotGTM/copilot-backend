from __future__ import annotations

from typing import List, Optional, Dict
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, HttpUrl, Field


class KnowledgeSourceListItem(BaseModel):
    source_id: UUID
    filename: Optional[str]
    mimetype: Optional[str]
    size_bytes: Optional[int]
    status: str
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None
    checksum: Optional[str] = None
    thumbnail_small_url: Optional[HttpUrl] = None
    thumbnail_medium_url: Optional[HttpUrl] = None

    model_config = {"from_attributes": True}


class KnowledgeSourcesPage(BaseModel):
    items: List[KnowledgeSourceListItem]
    cursor: Optional[str] = None


class KnowledgeDownloadResponse(BaseModel):
    source_id: UUID
    download_url: HttpUrl
    expires_in_seconds: int = Field(default=120 * 60)


class KnowledgeSourcesStats(BaseModel):
    total: int
    by_status: Dict[str, int]
    by_type: Dict[str, int]


class KnowledgeDeleteResponse(BaseModel):
    source_id: UUID
    status: str

    model_config = {"from_attributes": True}
