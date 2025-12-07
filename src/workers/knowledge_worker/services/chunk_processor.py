"""Chunk processor - processes chunks with multimodal enhancement and embeddings."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationDocument,
    OrganisationSourceStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.agents.embedding_service import get_embedding_service
from src.workers.knowledge_worker.models import ChunkData, ProcessingContext

if TYPE_CHECKING:
    from src.workers.knowledge_worker.services.image_processor import ImageProcessor
    from src.workers.knowledge_worker.services.table_processor import TableProcessor

logger = get_logger("chunk_processor")


class ChunkProcessor:
    """
    Processes chunks: extracts multimodal content, generates embeddings, creates documents.

    This class orchestrates the per-chunk processing pipeline:
    1. Process tables (if any) → summaries + stored images
    2. Process pictures (if any) → summaries + stored images
    3. Create enhanced content (text + summaries)
    4. Generate content hash for deduplication
    5. Generate embedding
    6. Create OrganisationDocument

    The processor is parser-agnostic - it works with normalized ChunkData
    regardless of the source document format (PDF, DOCX, TXT).

    Usage:
        processor = ChunkProcessor(
            table_processor=TableProcessor(blob_service),
            image_processor=ImageProcessor(blob_service),
        )

        documents = processor.process_all(
            chunks=parsed_content.chunks,
            dl_doc=parsed_content.metadata.get("docling_doc"),
            context=context,
            supports_tables=parsed_content.supports_tables,
            supports_images=parsed_content.supports_images,
        )
    """

    def __init__(
        self,
        table_processor: TableProcessor | None = None,
        image_processor: ImageProcessor | None = None,
        embedding_service: Any | None = None,
    ):
        """
        Initialize the chunk processor.

        Args:
            table_processor: Service for processing tables (optional)
            image_processor: Service for processing images (optional)
            embedding_service: Service for generating embeddings (defaults to global instance)
        """
        self.table_processor = table_processor
        self.image_processor = image_processor
        self.embedding_service = embedding_service or get_embedding_service()

    def process_all(
        self,
        chunks: list[ChunkData],
        context: ProcessingContext,
        *,
        dl_doc: Any = None,
        supports_tables: bool = False,
        supports_images: bool = False,
        publisher: Any | None = None,
    ) -> list[OrganisationDocument]:
        """
        Process all chunks and return OrganisationDocument objects.

        Args:
            chunks: List of ChunkData from parser
            context: Processing context with org/source/user info
            dl_doc: Docling document object (needed for table/image extraction)
            supports_tables: Whether to process tables
            supports_images: Whether to process images
            publisher: Optional ProgressPublisher for SSE updates (progress 20-80%)

        Returns:
            List of OrganisationDocument objects ready for storage
        """
        documents = []
        total = len(chunks)

        for idx, chunk in enumerate(chunks):
            try:
                doc = self.process_chunk(
                    chunk,
                    context,
                    dl_doc=dl_doc,
                    supports_tables=supports_tables,
                    supports_images=supports_images,
                )
                documents.append(doc)

                # Publish progress after each chunk (20% to 80% range)
                if publisher and total > 0:
                    progress = 20 + int((idx + 1) / total * 60)
                    publisher.publish(
                        status=OrganisationSourceStatus.EXTRACTING.value,
                        progress=progress,
                    )

                logger.debug(f"Processed chunk {chunk.index + 1}/{total}")

            except Exception as e:
                logger.error(f"Failed to process chunk {chunk.index}: {e}")
                continue

        logger.info(f"Processed {len(documents)}/{total} chunks successfully")
        return documents

    def process_chunk(
        self,
        chunk: ChunkData,
        context: ProcessingContext,
        *,
        dl_doc: Any = None,
        supports_tables: bool = False,
        supports_images: bool = False,
    ) -> OrganisationDocument:
        """
        Process a single chunk into an OrganisationDocument.

        This method was extracted from PDFParser._chunk_level_processing()
        and the main parse() loop (lines 119-175, 188-241 of old pdf_parser.py).

        Args:
            chunk: The ChunkData to process
            context: Processing context
            dl_doc: Docling document (for multimodal extraction)
            supports_tables: Whether to process tables
            supports_images: Whether to process images

        Returns:
            OrganisationDocument ready for database insertion
        """
        # 1. Process tables (if supported and present)
        table_summaries: list[str] = []
        table_images: list[dict[str, Any]] = []

        if supports_tables and self.table_processor and chunk.has_tables and dl_doc:
            table_summaries, table_images = self.table_processor.process_tables(
                chunk.tables, dl_doc, context
            )

        # 2. Process images (if supported and present)
        image_summaries: list[str] = []
        picture_images: list[dict[str, Any]] = []

        if supports_images and self.image_processor and chunk.has_images and dl_doc:
            image_summaries, picture_images = self.image_processor.process_images(
                chunk.pictures, dl_doc, context
            )

        # 3. Create enhanced content
        enhanced_content = self._create_enhanced_content(
            chunk.text, table_summaries, image_summaries
        )

        # 4. Generate content hash
        content_hash = self._compute_chunk_hash(
            enhanced_content,
            has_tables=chunk.has_tables,
            has_images=chunk.has_images,
            table_count=len(table_summaries),
            image_count=len(image_summaries),
        )

        # 5. Generate embedding
        embedding = self.embedding_service.get_embedding(enhanced_content)

        # 6. Build metadata
        meta_data = {
            "source_metadata": {
                "document_url": context.original_filename,
                "path": context.source_blob_path,
            },
            "chunk_metadata": {
                "chunk_index": chunk.index,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
            },
            "table_images_list": table_images,
            "images_list": picture_images,
        }

        # Include any parser-specific metadata
        if chunk.raw_metadata:
            meta_data["parser_metadata"] = chunk.raw_metadata

        # 7. Build filters
        filters = {
            "has_tables": chunk.has_tables,
            "has_images": chunk.has_images,
            "is_pdf": context.mimetype == "application/pdf" if context.mimetype else False,
        }

        # 8. Create OrganisationDocument
        return OrganisationDocument(
            source_id=UUID(context.source_id),
            uploader_user_id=UUID(context.user_id),
            name=context.original_filename,
            content=enhanced_content,
            embedding=embedding,
            meta_data=meta_data,
            filters=filters,
            content_hash=content_hash,
            is_org_wide=True,
            is_searchable=True,
            is_archived=False,
        )

    def _create_enhanced_content(
        self,
        text: str,
        table_summaries: list[str],
        image_summaries: list[str],
    ) -> str:
        """
        Create enhanced content by combining text with table and image summaries.

        This creates richer context for embedding generation that captures
        multimodal document information.

        Extracted from PDFParser._create_enhanced_content() (lines 401-420).

        Args:
            text: Clean text content from the chunk
            table_summaries: List of LLM-generated table summaries
            image_summaries: List of LLM-generated image summaries

        Returns:
            Combined enhanced content string
        """
        sections = [text.strip()]

        if table_summaries:
            table_context = "\n\n[TABLE CONTENT]\n" + "\n".join(
                f"Table {i + 1}: {summary}" for i, summary in enumerate(table_summaries)
            )
            sections.append(table_context)

        if image_summaries:
            image_context = "\n\n[VISUAL CONTENT]\n" + "\n".join(
                f"Image {i + 1}: {summary}" for i, summary in enumerate(image_summaries)
            )
            sections.append(image_context)

        return "\n".join(sections)

    def _compute_chunk_hash(
        self,
        enhanced_content: str,
        *,
        has_tables: bool,
        has_images: bool,
        table_count: int,
        image_count: int,
    ) -> str:
        """
        Compute SHA-256 hash for chunk content to detect duplicates.

        The hash includes metadata about multimodal content to ensure
        chunks with identical text but different tables/images are unique.

        Extracted from PDFParser._compute_chunk_hash() (lines 422-440).

        Args:
            enhanced_content: The combined text + summaries
            has_tables: Whether chunk contains tables
            has_images: Whether chunk contains images
            table_count: Number of table summaries
            image_count: Number of image summaries

        Returns:
            SHA-256 hex digest
        """
        hash_components = [
            enhanced_content,
            str(has_tables),
            str(has_images),
            str(table_count),
            str(image_count),
        ]
        content_to_hash = "|".join(hash_components)
        return hashlib.sha256(content_to_hash.encode("utf-8")).hexdigest()
