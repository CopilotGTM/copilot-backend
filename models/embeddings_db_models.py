# app_layer/models/embeddings_db_models.py
from typing import Any, Dict, Optional, List
from datetime import datetime

from sqlmodel import SQLModel, Field, Column
from sqlalchemy import text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector


class OrganisationDocument(SQLModel, table=True):
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
    created_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=text("now()")),
        default_factory=datetime.utcnow,
    )
    updated_at: datetime = Field(
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=text("now()"),
            server_onupdate=text("now()"),
        )
    )
    content_hash: Optional[str] = Field(default=None, index=True)
