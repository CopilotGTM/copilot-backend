# embeddings_db_models.py
import uuid
from uuid import UUID, uuid4
from datetime import datetime
from enum import Enum
from typing import List, Dict, Any, Optional

from pydantic import AnyUrl
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import String, func
from sqlalchemy.dialects.postgresql import JSONB, ENUM
from pgvector.sqlalchemy import Vector


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

    document_id: UUID = Field(default_factory=uuid4, primary_key=True, description="Primary key UUID for the document.")
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

    # JSONB columns for metadata
    origin_metadata: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Free-form JSON metadata (page numbers, tags, OCR data).",
    )
    document_metadata: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Free-form JSON metadata (page numbers, tags, OCR data).",
    )

    # URL stored as plain string
    source_url: Optional[AnyUrl] = Field(
        default=None, sa_column=Column(String), description="Link back to the original document, if available."
    )

    # pgvector embedding column
    embedding: List[float] = Field(
        sa_column=Column(
            Vector(1536),
            index=True,
            info={"vector_index_type": "hnsw", "vector_index_params": {"m": 16, "ef_construction": 200}},
        ),
        description="1536‑dim semantic embedding for vector search.",
    )

    created_at: datetime = Field(
        sa_column=Column(default=func.now(), nullable=False), description="Timestamp when the document was created."
    )
    updated_at: datetime = Field(
        sa_column=Column(default=func.now(), onupdate=func.now(), nullable=False),
        description="Timestamp when the document was last updated.",
    )

    class Config:
        from_attributes = True  # formerly orm_mode
