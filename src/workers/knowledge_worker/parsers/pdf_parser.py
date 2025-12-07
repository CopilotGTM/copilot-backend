"""PDF Parser - Clean implementation using IDocumentParser protocol.

This module provides the primary PDF parser that:
1. Implements IDocumentParser protocol
2. Returns ParsedContent (raw chunks)
3. Does NOT handle embeddings or storage (delegated to ChunkProcessor)

This is designed to work with the ExtractionService pipeline:
    PDFParser → ParsedContent → ChunkProcessor → OrganisationDocument[]
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from docling_core.types.doc import DocItemLabel, PictureItem, TableItem

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.models import ChunkData, ParsedContent, ProcessingContext
from src.workers.knowledge_worker.parsers.protocol import IDocumentParser
from src.workers.knowledge_worker.parsers.registry import ParserRegistry
from src.workers.knowledge_worker.services.chunking import (
    DEFAULT_EMBED_MODEL_ID,
    get_hybrid_chunker,
)
from src.workers.knowledge_worker.services.document_converter import get_doc_converter
from src.workers.knowledge_worker.utils.content_processor import ContentProcessor

if TYPE_CHECKING:
    from docling.chunking import HybridChunker
    from docling.document_converter import DocumentConverter

logger = get_logger("pdf_parser")


@ParserRegistry.register
class PDFParser(IDocumentParser):
    """
    PDF parser implementing the IDocumentParser protocol.

    This parser is responsible ONLY for:
    1. Converting PDF to structured representation (via Docling)
    2. Chunking the document
    3. Extracting text and multimodal references (tables, images)

    It does NOT handle:
    - Embeddings (delegated to ChunkProcessor)
    - Storage (delegated to ChunkProcessor via TableProcessor/ImageProcessor)
    - OrganisationDocument creation (delegated to ChunkProcessor)

    Design:
        The parser uses lazy initialization for heavy resources (converter, chunker)
        to avoid loading them until actually needed.

    Usage:
        parser = PDFParser()
        result = parser.parse(file_path, context)

        # result.chunks contains ChunkData with:
        #   - text: clean text content
        #   - tables: list of TableItem references
        #   - pictures: list of PictureItem references
        # result.metadata["docling_doc"] contains the Docling document
    """

    def __init__(self):
        """Initialize PDF parser with lazy-loaded resources."""
        self._converter: DocumentConverter | None = None
        self._chunker: HybridChunker | None = None

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Return MIME types this parser supports."""
        return ["application/pdf"]

    @property
    def converter(self) -> Any:
        """Lazy-load document converter."""
        if self._converter is None:
            self._converter = get_doc_converter()
        return self._converter

    @property
    def chunker(self) -> Any:
        """Lazy-load hybrid chunker."""
        if self._chunker is None:
            self._chunker = get_hybrid_chunker(DEFAULT_EMBED_MODEL_ID)
        return self._chunker

    def parse(
        self,
        file_path: str | Path,
        context: ProcessingContext,
    ) -> ParsedContent:
        """
        Parse a PDF file and return structured content.

        Args:
            file_path: Path to the PDF file
            context: Processing context with org/source/user info

        Returns:
            ParsedContent with chunks and metadata
        """
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

        logger.info(f"Parsing PDF: {file_path.name} (source: {context.source_id})")

        try:
            # 1. Convert PDF using Docling
            result = self.converter.convert(str(file_path))
            dl_doc = result.document

            # 2. Chunk the document
            raw_chunks = list(self.chunker.chunk(dl_doc=dl_doc))
            logger.info(f"Docling produced {len(raw_chunks)} chunks")

            if not raw_chunks:
                logger.warning(f"No chunks produced for {file_path}")
                return ParsedContent(
                    chunks=[],
                    metadata={"docling_doc": dl_doc},
                    supports_tables=True,
                    supports_images=True,
                )

            # 3. Convert to ChunkData objects
            chunks = []
            for index, raw_chunk in enumerate(raw_chunks):
                chunk_data = self._convert_chunk(index, raw_chunk, dl_doc)
                chunks.append(chunk_data)

            logger.info(
                f"Parsed {len(chunks)} chunks from {file_path.name} "
                f"({sum(1 for c in chunks if c.has_tables)} with tables, "
                f"{sum(1 for c in chunks if c.has_images)} with images)"
            )

            return ParsedContent(
                chunks=chunks,
                metadata={"docling_doc": dl_doc},
                supports_tables=True,
                supports_images=True,
            )

        except Exception as e:
            logger.error(f"Failed to parse PDF {file_path}: {e}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

    def _convert_chunk(
        self,
        index: int,
        raw_chunk: Any,
        dl_doc: Any,
    ) -> ChunkData:
        """
        Convert a Docling chunk to ChunkData.

        Args:
            index: Chunk index
            raw_chunk: Raw chunk from HybridChunker
            dl_doc: Docling document object

        Returns:
            ChunkData with extracted information
        """
        # Get contextualized text and clean it
        text = self.chunker.contextualize(raw_chunk)
        clean_text = ContentProcessor.clean_text(text)

        # Extract page numbers
        page_start, page_end = self._extract_page_numbers(raw_chunk)

        # Extract doc_items (tables and images)
        doc_items = list(getattr(raw_chunk.meta, "doc_items", []) or [])

        # Separate tables and pictures
        tables = self._extract_tables(doc_items, dl_doc)
        pictures = self._extract_pictures(doc_items, dl_doc)

        return ChunkData(
            index=index,
            text=clean_text,
            page_start=page_start,
            page_end=page_end,
            tables=tables,
            pictures=pictures,
            raw_metadata={
                "doc_items_count": len(doc_items),
            },
        )

    def _extract_page_numbers(self, chunk: Any) -> tuple[int | None, int | None]:
        """
        Extract page numbers from chunk provenance.

        Args:
            chunk: Docling chunk object

        Returns:
            Tuple of (page_start, page_end)
        """
        doc_items = getattr(chunk.meta, "doc_items", []) or []
        pages = set()

        for item in doc_items:
            for prov in getattr(item, "prov", []):
                page_no = getattr(prov, "page_no", None)
                if page_no is not None:
                    pages.add(page_no)

        if not pages:
            return None, None

        return min(pages), max(pages)

    def _extract_tables(
        self,
        doc_items: list[Any],
        dl_doc: Any,  # noqa: ARG002 - kept for signature consistency
    ) -> list[TableItem]:
        """
        Extract table items from doc_items.

        Args:
            doc_items: List of doc items from chunk
            dl_doc: Docling document (unused, for signature consistency)

        Returns:
            List of TableItem objects
        """
        tables = []
        for item in doc_items:
            is_table = isinstance(item, TableItem) or (
                hasattr(item, "label") and item.label == DocItemLabel.TABLE
            )
            if is_table:
                tables.append(item)
        return tables

    def _extract_pictures(
        self,
        doc_items: list[Any],
        dl_doc: Any,  # noqa: ARG002 - kept for signature consistency
    ) -> list[PictureItem]:
        """
        Extract picture items from doc_items.

        Args:
            doc_items: List of doc items from chunk
            dl_doc: Docling document (unused, for signature consistency)

        Returns:
            List of PictureItem objects
        """
        pictures = []
        for item in doc_items:
            is_picture = isinstance(item, PictureItem) or (
                hasattr(item, "label") and item.label == DocItemLabel.PICTURE
            )
            if is_picture:
                pictures.append(item)
        return pictures
