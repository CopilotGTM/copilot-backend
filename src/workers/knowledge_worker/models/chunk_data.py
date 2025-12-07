"""Chunk data model representing parsed document chunks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChunkData:
    """
    Raw chunk data from a parser (before enhancement).

    This is the normalized output format that all parsers must produce.
    The ChunkProcessor will then enhance this with:
    - Table summaries (if tables present)
    - Image summaries (if pictures present)
    - Embeddings
    - Content hash

    Attributes:
        index: Chunk sequence number within the document
        text: Cleaned text content of the chunk
        page_start: First page number (1-indexed), None for non-paginated docs
        page_end: Last page number (1-indexed), None for non-paginated docs
        tables: List of table objects (Docling TableItem or similar)
        pictures: List of picture objects (Docling PictureItem or similar)
        raw_metadata: Any parser-specific metadata to pass through
    """

    index: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    tables: list[Any] = field(default_factory=list)
    pictures: list[Any] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tables(self) -> bool:
        """Check if this chunk contains tables."""
        return bool(self.tables)

    @property
    def has_images(self) -> bool:
        """Check if this chunk contains pictures/images."""
        return bool(self.pictures)

    def __repr__(self) -> str:
        return (
            f"ChunkData(index={self.index}, "
            f"pages={self.page_start}-{self.page_end}, "
            f"tables={len(self.tables)}, "
            f"pictures={len(self.pictures)}, "
            f"text_len={len(self.text)})"
        )
