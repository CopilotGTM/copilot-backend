"""JSON Parser - Structure-aware parser for JSON files.

This module provides a parser for JSON files that:
1. Implements IDocumentParser protocol
2. Uses structure-aware chunking (splits at top-level keys/array items)
3. Preserves JSON structure for LLM understanding
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.models import ChunkData, ParsedContent, ProcessingContext
from src.workers.knowledge_worker.parsers.protocol import IDocumentParser
from src.workers.knowledge_worker.parsers.registry import ParserRegistry

logger = get_logger("json_parser")

# Maximum size for a single chunk (in characters)
MAX_CHUNK_SIZE = 4000


@ParserRegistry.register
class JsonParser(IDocumentParser):
    """
    Structure-aware parser for JSON files.

    Chunking Strategy:
    1. Small JSON (< MAX_CHUNK_SIZE): Return as single chunk.
    2. JSON Object: Split by top-level keys. Each key-value pair becomes a chunk.
    3. JSON Array: Split by array items. Each item becomes a chunk.
    4. If a single key/item is still too large, it becomes its own chunk (may exceed limit).

    This ensures we never break in the middle of a JSON structure.
    """

    def __init__(self, max_chunk_size: int = MAX_CHUNK_SIZE):
        self.max_chunk_size = max_chunk_size

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Return MIME types this parser supports."""
        return ["application/json"]

    def parse(
        self,
        file_path: str | Path,
        context: ProcessingContext,
    ) -> ParsedContent:
        """Parse a JSON file and return structured content."""
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

        logger.info(f"Parsing JSON file: {file_path.name} (source: {context.source_id})")

        try:
            content = file_path.read_text(encoding="utf-8")

            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON in {file_path}: {e}, treating as plain text")
                # Fallback: treat as plain text with single chunk
                return ParsedContent(
                    chunks=[
                        ChunkData(
                            index=0,
                            text=content,
                            page_start=None,
                            page_end=None,
                            tables=[],
                            pictures=[],
                        )
                    ],
                    metadata={"error": "invalid_json", "original_size": len(content)},
                    supports_tables=False,
                    supports_images=False,
                )

            # Structure-aware chunking
            chunks = self._create_structure_aware_chunks(data)

            logger.info(f"Parsed {len(chunks)} chunks from {file_path.name}")

            return ParsedContent(
                chunks=chunks,
                metadata={
                    "original_size": len(content),
                    "chunk_count": len(chunks),
                    "json_type": (
                        "object"
                        if isinstance(data, dict)
                        else "array"
                        if isinstance(data, list)
                        else "primitive"
                    ),
                },
                supports_tables=False,
                supports_images=False,
            )

        except Exception as e:
            logger.error(f"Failed to parse JSON file {file_path}: {e}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

    def _create_structure_aware_chunks(self, data: Any) -> list[ChunkData]:
        """
        Create chunks based on JSON structure.

        Strategy:
        - If entire JSON fits in max_chunk_size, return as single chunk.
        - If JSON is an object, split by top-level keys.
        - If JSON is an array, split by array items.
        - Group small items together until they approach max_chunk_size.
        """
        full_json = json.dumps(data, indent=2, ensure_ascii=False)

        # Small JSON: single chunk
        if len(full_json) <= self.max_chunk_size:
            return [
                ChunkData(
                    index=0,
                    text=full_json,
                    page_start=None,
                    page_end=None,
                    tables=[],
                    pictures=[],
                )
            ]

        chunks: list[ChunkData] = []

        if isinstance(data, dict):
            chunks = self._chunk_object(data)
        elif isinstance(data, list):
            chunks = self._chunk_array(data)
        else:
            # Primitive value (string, number, etc.) - single chunk
            chunks = [
                ChunkData(
                    index=0,
                    text=full_json,
                    page_start=None,
                    page_end=None,
                    tables=[],
                    pictures=[],
                )
            ]

        return chunks

    def _chunk_object(self, data: dict) -> list[ChunkData]:
        """Split a JSON object by top-level keys, grouping small ones together."""
        chunks: list[ChunkData] = []
        current_items: dict[str, Any] = {}
        current_size = 2  # Start with "{}" overhead

        for key, value in data.items():
            item_json = json.dumps({key: value}, indent=2, ensure_ascii=False)
            item_size = len(item_json)

            # If adding this item would exceed limit, flush current batch
            if current_items and (current_size + item_size > self.max_chunk_size):
                chunks.append(self._create_chunk(len(chunks), current_items))
                current_items = {}
                current_size = 2

            current_items[key] = value
            current_size += item_size

        # Flush remaining items
        if current_items:
            chunks.append(self._create_chunk(len(chunks), current_items))

        return chunks

    def _chunk_array(self, data: list) -> list[ChunkData]:
        """Split a JSON array by items, grouping small ones together."""
        chunks: list[ChunkData] = []
        current_items: list[Any] = []
        current_size = 2  # Start with "[]" overhead

        for item in data:
            item_json = json.dumps(item, indent=2, ensure_ascii=False)
            item_size = len(item_json)

            # If adding this item would exceed limit, flush current batch
            if current_items and (current_size + item_size > self.max_chunk_size):
                chunks.append(self._create_chunk(len(chunks), current_items))
                current_items = []
                current_size = 2

            current_items.append(item)
            current_size += item_size

        # Flush remaining items
        if current_items:
            chunks.append(self._create_chunk(len(chunks), current_items))

        return chunks

    def _create_chunk(self, index: int, data: dict | list) -> ChunkData:
        """Create a ChunkData from a JSON structure."""
        text = json.dumps(data, indent=2, ensure_ascii=False)
        return ChunkData(
            index=index,
            text=text,
            page_start=None,
            page_end=None,
            tables=[],
            pictures=[],
        )
