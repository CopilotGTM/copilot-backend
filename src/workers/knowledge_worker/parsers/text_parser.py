"""Text Parser - Simple parser for plain text files.

This module provides a text parser that:
1. Implements IDocumentParser protocol
2. Returns ParsedContent with text chunks
3. Does NOT support tables or images (text files are single-modal)

This demonstrates the multi-parser architecture with a simple case.
"""

from __future__ import annotations

from pathlib import Path

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.models import ChunkData, ParsedContent, ProcessingContext
from src.workers.knowledge_worker.parsers.protocol import IDocumentParser
from src.workers.knowledge_worker.parsers.registry import ParserRegistry
from src.workers.knowledge_worker.utils.content_processor import ContentProcessor

logger = get_logger("text_parser")

# Default chunk size for text splitting (in characters)
DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 200


@ParserRegistry.register
class TextParser(IDocumentParser):
    """
    Parser for plain text files.

    This parser handles simple text files by:
    1. Reading the file content
    2. Splitting into chunks of manageable size
    3. Returning ParsedContent with ChunkData objects

    Configuration:
        chunk_size: Maximum characters per chunk (default: 1500)
        chunk_overlap: Overlap between chunks (default: 200)

    Usage:
        parser = TextParser()
        result = parser.parse(file_path, context)
    """

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        """
        Initialize text parser with chunking parameters.

        Args:
            chunk_size: Maximum characters per chunk
            chunk_overlap: Overlap between consecutive chunks
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Return MIME types this parser supports."""
        return [
            "text/plain",
            "text/markdown",
            "text/csv",
            "text/html",
            "application/json",
            "application/xml",
            "text/xml",
        ]

    def parse(
        self,
        file_path: str | Path,
        context: ProcessingContext,
    ) -> ParsedContent:
        """
        Parse a text file and return structured content.

        Args:
            file_path: Path to the text file
            context: Processing context with org/source/user info

        Returns:
            ParsedContent with text chunks (no tables/images)
        """
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

        logger.info(f"Parsing text file: {file_path.name} (source: {context.source_id})")

        try:
            # Read file with encoding detection fallback
            content = self._read_file(file_path)

            if not content or not content.strip():
                logger.warning(f"Empty content in {file_path}")
                return ParsedContent(
                    chunks=[],
                    metadata={"original_size": 0},
                    supports_tables=False,
                    supports_images=False,
                )

            # Clean the content
            clean_content = ContentProcessor.clean_text(content)

            # Split into chunks
            chunks = self._split_into_chunks(clean_content)

            logger.info(f"Parsed {len(chunks)} chunks from {file_path.name}")

            return ParsedContent(
                chunks=chunks,
                metadata={
                    "original_size": len(content),
                    "cleaned_size": len(clean_content),
                    "chunk_count": len(chunks),
                },
                supports_tables=False,  # Text files don't have tables
                supports_images=False,  # Text files don't have images
            )

        except Exception as e:
            logger.error(f"Failed to parse text file {file_path}: {e}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

    def _read_file(self, file_path: Path) -> str:
        """
        Read file content with encoding detection.

        Tries UTF-8 first, then falls back to latin-1.

        Args:
            file_path: Path to the file

        Returns:
            File content as string
        """
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.debug(f"UTF-8 failed for {file_path}, trying latin-1")
            return file_path.read_text(encoding="latin-1")

    def _split_into_chunks(self, content: str) -> list[ChunkData]:
        """
        Split content into overlapping chunks.

        Uses a simple character-based splitting strategy that tries to
        break at sentence boundaries when possible.

        Args:
            content: Clean text content

        Returns:
            List of ChunkData objects
        """
        if len(content) <= self.chunk_size:
            # Single chunk for small content
            return [
                ChunkData(
                    index=0,
                    text=content,
                    page_start=None,  # Text files don't have pages
                    page_end=None,
                    tables=[],
                    pictures=[],
                )
            ]

        chunks = []
        start = 0
        index = 0

        while start < len(content):
            # Calculate end position
            end = start + self.chunk_size

            if end < len(content):
                # Try to find a good break point (sentence end)
                break_point = self._find_break_point(content, start, end)
                if break_point > start:
                    end = break_point

            chunk_text = content[start:end].strip()

            if chunk_text:
                chunks.append(
                    ChunkData(
                        index=index,
                        text=chunk_text,
                        page_start=None,
                        page_end=None,
                        tables=[],
                        pictures=[],
                        raw_metadata={
                            "char_start": start,
                            "char_end": end,
                        },
                    )
                )
                index += 1

            # Move start with overlap
            start = end - self.chunk_overlap
            if start <= chunks[-1].raw_metadata.get("char_start", 0) if chunks else 0:
                # Avoid infinite loop
                start = end

        return chunks

    def _find_break_point(self, content: str, start: int, end: int) -> int:
        """
        Find a good break point near the end position.

        Looks for sentence endings (., !, ?) followed by whitespace.

        Args:
            content: Full content string
            start: Chunk start position
            end: Target end position

        Returns:
            Best break point position
        """
        # Search backwards from end for a sentence boundary
        search_start = max(start, end - 200)  # Look back up to 200 chars
        search_text = content[search_start:end]

        # Find last sentence ending
        best_break = -1
        for i, char in enumerate(search_text):
            if char in ".!?" and i + 1 < len(search_text):
                next_char = search_text[i + 1]
                if next_char.isspace() or next_char in "\"'":
                    best_break = search_start + i + 1

        if best_break > start:
            return best_break

        # Fall back to paragraph break
        for i in range(len(search_text) - 1, -1, -1):
            if search_text[i] == "\n":
                return search_start + i + 1

        # No good break found, use original end
        return end
