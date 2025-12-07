"""Parser protocol - interface that all document parsers must implement."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from src.workers.knowledge_worker.models import ParsedContent, ProcessingContext


@runtime_checkable
class IDocumentParser(Protocol):
    """
    Interface that all document parsers must implement.

    This protocol defines the contract for document parsing:
    1. A parser declares which MIME types it supports via `supported_mimetypes()`
    2. A parser converts a file into normalized `ParsedContent` via `parse()`

    The parser is ONLY responsible for:
    - Converting the document format (PDF, DOCX, TXT)
    - Chunking the content
    - Extracting raw tables/images (without summarization)

    The parser is NOT responsible for:
    - LLM summarization (handled by ChunkProcessor)
    - Embedding generation (handled by ChunkProcessor)
    - Blob storage uploads (handled by TableProcessor/ImageProcessor)
    - Database operations (handled by DocumentStore)

    Example Implementation:
        @ParserRegistry.register
        class PDFParser(IDocumentParser):
            @classmethod
            def supported_mimetypes(cls) -> list[str]:
                return ["application/pdf"]

            def parse(self, file_path: Path, context: ProcessingContext) -> ParsedContent:
                # ... parsing logic ...
                return ParsedContent(chunks=[...], supports_tables=True)
    """

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """
        Return the list of MIME types this parser can handle.

        Returns:
            List of MIME type strings (lowercase), e.g., ["application/pdf"]
        """
        ...

    def parse(self, file_path: Path, context: ProcessingContext) -> ParsedContent:
        """
        Parse a document file and return normalized content.

        Args:
            file_path: Path to the document file on disk
            context: Processing context with org/source/user info

        Returns:
            ParsedContent with chunks and metadata

        Raises:
            Exception: If parsing fails (will be caught by ExtractionService)
        """
        ...


class UnsupportedMimeTypeError(Exception):
    """Raised when no parser is registered for a MIME type."""

    def __init__(self, mimetype: str):
        self.mimetype = mimetype
        super().__init__(f"No parser registered for MIME type: {mimetype}")
