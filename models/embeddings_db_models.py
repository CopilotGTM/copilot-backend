# embeddings_db_models.py

import uuid
from uuid import UUID, uuid4
from datetime import datetime
from enum import Enum
from typing import List, Dict, Any, Optional

from pydantic import AnyUrl
from sqlmodel import SQLModel, Field, Column, String
from sqlalchemy.dialects.postgresql import JSONB, ENUM
from pgvector.sqlalchemy import Vector
from sqlalchemy import Index


class ContentType(str, Enum):
    text = "text"
    image = "image"
    ppt = "ppt"
    pdf = "pdf"
    audio = "audio"
    video = "video"
    table = "table"


class OrganisationDocument(SQLModel, table=True):
    __tablename__ = "organisation_documents"

    document_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key UUID for the document.",
    )

    organisation_id: UUID = Field(
        foreign_key="organisations.organisation_id",
        nullable=False,
        index=True,
        description="Foreign key to the owning organisation.",
    )

    title: str = Field(..., description="Short title or filename of the document.")

    content_type: ContentType = Field(
        default=ContentType.text,
        sa_column=Column(ENUM(ContentType, name="content_type_enum", create_type=False), nullable=False, index=True),
        description="Type of content (text, image, ppt, etc.)",
    )

    origin_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Free-form JSON metadata (page numbers, tags, OCR data).",
    )
    document_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Free-form JSON metadata (page numbers, tags, OCR data).",
    )

    source_url: Optional[AnyUrl] = Field(
        default=None,
        sa_column=Column(String),
        description="Link back to the original document, if available.",
    )

    embedding: List[float] = Field(
        sa_column=Column(
            Vector(1536),
            nullable=False,
        ),
        description="1536-dim semantic embedding for vector search.",
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow, nullable=False, description="Timestamp when the document was created."
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, nullable=False, description="Timestamp when the document was last updated."
    )

    # Define HNSW index for vector similarity search
    __table_args__ = (
        Index(
            "organisation_documents_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={
                "m": 16,  # Number of connections per layer
                "ef_construction": 64,  # Size of dynamic candidate list for construction
            },
        ),
    )

    class Config:
        from_attributes = True  # formerly orm_mode
