from typing import Any, Dict, Optional, List
from datetime import datetime

from sqlmodel import SQLModel, Field, Column
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from .base_mixins import TimestampMixin


class OrganisationDocument(TimestampMixin, table=True):
    __tablename__ = "knowledge_base"

    id: str = Field(primary_key=True, index=True)
    name: Optional[str] = Field(default=None, index=True)
    meta_data: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
    )
    filters: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
    )
    content: Optional[str] = Field(default=None)
    embedding: Optional[List[float]] = Field(
        sa_column=Column(Vector),
        default=None,
    )
    usage: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
    )
    content_hash: Optional[str] = Field(default=None, index=True)