"""Parsed content model - output from document parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chunk_data import ChunkData


@dataclass
class ParsedContent:
    """
    Output from any document parser - normalized structure.

    All parsers (PDF, Text, DOCX, etc.) must return this format.
    This allows the ChunkProcessor to handle all document types uniformly.

    Attributes:
        chunks: List of ChunkData objects representing document segments
        metadata: Parser-specific metadata (e.g., docling_doc for PDF, page_count, etc.)
        supports_tables: Whether this parser extracted table structures
        supports_images: Whether this parser extracted images/pictures
    """

    chunks: list[ChunkData]
    metadata: dict[str, Any] = field(default_factory=dict)
    supports_tables: bool = False
    supports_images: bool = False

    @property
    def chunk_count(self) -> int:
        """Return the number of chunks."""
        return len(self.chunks)

    @property
    def is_empty(self) -> bool:
        """Check if no content was extracted."""
        return len(self.chunks) == 0

    @property
    def total_tables(self) -> int:
        """Count total tables across all chunks."""
        return sum(len(chunk.tables) for chunk in self.chunks)

    @property
    def total_images(self) -> int:
        """Count total images across all chunks."""
        return sum(len(chunk.pictures) for chunk in self.chunks)

    def __repr__(self) -> str:
        return (
            f"ParsedContent(chunks={self.chunk_count}, "
            f"tables={self.total_tables}, "
            f"images={self.total_images}, "
            f"supports_tables={self.supports_tables}, "
            f"supports_images={self.supports_images})"
        )
