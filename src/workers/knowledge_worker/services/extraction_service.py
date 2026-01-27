"""Extraction service - orchestrates the full document extraction pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationDocument,
    OrganisationSourceStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.models import ExtractionResult, ProcessingContext
from src.workers.knowledge_worker.parsers.registry import ParserRegistry
from src.workers.knowledge_worker.services.chunk_processor import (
    DEFAULT_MAX_PARALLEL_CHUNKS,
    ChunkProcessor,
)
from src.workers.knowledge_worker.services.image_processor import ImageProcessor
from src.workers.knowledge_worker.services.table_processor import TableProcessor
from src.workers.knowledge_worker.utils import DocumentStore

if TYPE_CHECKING:
    from uuid import UUID

logger = get_logger("extraction_service")

# Environment variable to configure parallel chunk processing
# Set to 1 to disable parallel processing
ENV_MAX_PARALLEL_CHUNKS = "KNOWLEDGE_EXTRACTION_MAX_PARALLEL_CHUNKS"


class ExtractionService:
    """
    High-level service that orchestrates the complete document extraction pipeline.

    This service is the primary interface for the document_extraction Celery task.
    It coordinates:
    1. Parser selection (via ParserRegistry)
    2. Document parsing → ParsedContent
    3. Chunk processing (multimodal + embeddings) → OrganisationDocument[]
    4. Result handling

    The service is stateless and can be instantiated per-request or as a singleton.

    Design Rationale:
    -----------------
    This service extracts orchestration logic from document_extraction.py and
    parser_factory.py, providing a clean separation between:
    - Task scheduling (Celery task)
    - Pipeline orchestration (this service)
    - Parsing (IDocumentParser implementations)
    - Chunk processing (ChunkProcessor)
    - Storage (DocumentStore)

    Usage:
        service = ExtractionService(organisation_id)
        result = service.extract(file_path, context)

        if result.success:
            document_store.store_documents(result.documents, ...)
        else:
            handle_error(result.error_message)
    """

    def __init__(
        self,
        organisation_id: str | UUID,
        storage_backend: Any | None = None,
        chunk_processor: ChunkProcessor | None = None,
        max_parallel_chunks: int | None = None,
    ):
        """
        Initialize the extraction service.

        Uses a persistent thread-local event loop via run_async() to prevent
        memory leaks from creating new event loops per task.

        Args:
            organisation_id: The organisation ID for storage paths
            storage_backend: Azure blob storage backend (for legacy parsers)
            chunk_processor: Optional pre-configured ChunkProcessor (for testing)
            max_parallel_chunks: Maximum chunks to process in parallel.
                                 Defaults to KNOWLEDGE_EXTRACTION_MAX_PARALLEL_CHUNKS env var,
                                 or DEFAULT_MAX_PARALLEL_CHUNKS (10) if not set.
                                 Set to 1 to disable parallel processing.
        """
        self.organisation_id = str(organisation_id)
        self.storage_backend = storage_backend

        # Determine max parallel chunks from parameter, env var, or default
        if max_parallel_chunks is None:
            env_value = os.environ.get(ENV_MAX_PARALLEL_CHUNKS)
            if env_value:
                try:
                    max_parallel_chunks = int(env_value)
                except ValueError:
                    logger.warning(
                        f"Invalid {ENV_MAX_PARALLEL_CHUNKS} value '{env_value}', "
                        f"using default {DEFAULT_MAX_PARALLEL_CHUNKS}"
                    )
                    max_parallel_chunks = DEFAULT_MAX_PARALLEL_CHUNKS
            else:
                max_parallel_chunks = DEFAULT_MAX_PARALLEL_CHUNKS

        # Initialize services
        if chunk_processor:
            self.chunk_processor = chunk_processor
        else:
            # Create default ChunkProcessor with all services using direct blob storage
            storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
            self.chunk_processor = ChunkProcessor(
                table_processor=TableProcessor(storage),
                image_processor=ImageProcessor(storage),
                max_parallel_chunks=max_parallel_chunks,
            )

    def extract(
        self,
        file_path: str | Path,
        context: ProcessingContext,
        publisher: Any | None = None,
    ) -> ExtractionResult:
        """
        Extract content from a document file.

        This is the main entry point for document extraction. It:
        1. Selects the appropriate parser based on MIME type
        2. Parses the document to get ParsedContent
        3. Processes chunks (multimodal + embeddings)
        4. Returns OrganisationDocument objects ready for storage

        Args:
            file_path: Path to the downloaded document file
            context: Processing context with org/source/user info
            publisher: Optional ProgressPublisher for SSE updates

        Returns:
            ExtractionResult with documents on success, error on failure
        """
        mimetype = context.mimetype
        if not mimetype:
            return ExtractionResult.fail("No MIME type specified in context")

        # 1. Get parser for this MIME type
        try:
            parser = ParserRegistry.get_parser(mimetype)
        except Exception as e:
            logger.warning(f"No parser found for MIME type {mimetype}: {e}")
            return ExtractionResult.fail(f"Unsupported document type: {mimetype}")

        logger.info(
            f"Using parser {parser.__class__.__name__} for {mimetype} (source: {context.source_id})"
        )

        # 2. Parse the document
        try:
            parsed_content = parser.parse(file_path, context)
        except Exception as e:
            logger.error(f"Parser failed for {file_path}: {e}")
            return ExtractionResult.fail(f"Failed to parse document: {e}")

        if not parsed_content or not parsed_content.chunks:
            logger.warning(f"No content extracted from {file_path}")
            return ExtractionResult.fail("No content could be extracted from document")

        # Progress: 20% after parsing
        if publisher:
            publisher.publish(status=OrganisationSourceStatus.EXTRACTING.value, progress=20)

        logger.info(f"Parsed {len(parsed_content.chunks)} chunks from {context.original_filename}")

        # 3. Process chunks → OrganisationDocument[]
        try:
            documents = self.chunk_processor.process_all(
                chunks=parsed_content.chunks,
                context=context,
                dl_doc=parsed_content.metadata.get("docling_doc"),
                supports_tables=parsed_content.supports_tables,
                supports_images=parsed_content.supports_images,
                publisher=publisher,
            )
        except Exception as e:
            logger.error(f"Chunk processing failed: {e}")
            return ExtractionResult.fail(f"Failed to process document content: {e}")

        if not documents:
            logger.warning(f"No documents generated from {len(parsed_content.chunks)} chunks")
            return ExtractionResult.fail("Failed to generate document embeddings")

        logger.info(
            f"Successfully processed {len(documents)} documents from {context.original_filename}"
        )

        return ExtractionResult.ok([str(doc.document_id) for doc in documents], documents=documents)

    def extract_and_store(
        self,
        file_path: str | Path,
        context: ProcessingContext,
        publisher: Any | None = None,
    ) -> ExtractionResult:
        """
        Extract content from a document and store it in the database.

        This is the complete pipeline entry point that:
        1. Extracts content using extract()
        2. Deletes any existing chunks for the source (idempotent re-run)
        3. Stores new documents in the database

        Args:
            file_path: Path to the downloaded document file
            context: Processing context with org/source/user info
            publisher: Optional ProgressPublisher for SSE updates

        Returns:
            ExtractionResult with stored document IDs on success, error on failure
        """
        # 1. Extract content (with progress updates if publisher provided)
        result = self.extract(file_path, context, publisher)

        if not result.success:
            return result

        documents = result.documents or []
        if not documents:
            return ExtractionResult.fail("No content could be extracted from document")

        # 2. Store documents (90% progress)
        if publisher:
            publisher.publish(status=OrganisationSourceStatus.EXTRACTING.value, progress=90)

        document_store = DocumentStore(self.organisation_id)

        # Delete existing chunks (idempotent re-run)
        try:
            deleted = document_store.delete_documents_by_source(context.source_id)
            if deleted:
                logger.info(
                    "Deleted %s existing chunks for source %s",
                    deleted,
                    context.source_id,
                )
        except Exception as e:
            logger.warning(
                "Failed to delete existing chunks for source %s: %s",
                context.source_id,
                e,
            )

        # Store new documents
        stored_ids = document_store.store_documents(
            documents,
            context.source_id,
            context.user_id,
        )

        if not stored_ids:
            logger.warning(
                "No new unique document chunks stored for source %s (all duplicates)",
                context.source_id,
            )
            # Still a success - just no new unique chunks
            return ExtractionResult.ok([], documents=[])

        logger.info(
            "Successfully stored %d document chunks for source %s",
            len(stored_ids),
            context.source_id,
        )

        return ExtractionResult.ok(stored_ids, documents=documents)

    def extract_legacy(
        self,
        file_path: str | Path,
        mimetype: str,
        user_id: str,
        document_url: str | None = None,
        source_id: str | None = None,
        source_blob_path: str | None = None,
        original_filename: str | None = None,
        **kwargs,  # Accept extra args for API compatibility (e.g., source_size_bytes)
    ) -> list[OrganisationDocument]:
        """
        Legacy extraction method for backward compatibility with parser_factory.

        This method maintains the same interface as parser_factory.parse_document()
        to allow gradual migration of existing code.

        DEPRECATED: Use extract() with ProcessingContext instead.

        Args:
            file_path: Path to the document file
            mimetype: The MIME type of the document
            user_id: The user ID who uploaded the document
            document_url: Optional URL of the document
            source_id: Source record ID
            source_blob_path: Blob storage path
            original_filename: Original filename
            **kwargs: Additional arguments (ignored, for backward compatibility)

        Returns:
            List of OrganisationDocument objects (empty on failure)
        """
        # Log any unused kwargs for debugging during migration
        if kwargs:
            logger.debug(f"extract_legacy received unused kwargs: {list(kwargs.keys())}")
        # Build ProcessingContext from legacy parameters
        context = ProcessingContext(
            organisation_id=self.organisation_id,
            source_id=source_id or "",
            user_id=user_id,
            source_blob_path=source_blob_path,
            original_filename=original_filename or document_url,
            mimetype=mimetype,
        )

        result = self.extract(file_path, context)

        if result.success and result.documents:
            return result.documents
        else:
            if result.error_message:
                logger.error(f"Legacy extraction failed: {result.error_message}")
            return []

    def get_supported_mimetypes(self) -> list[str]:
        """Get list of all supported MIME types."""
        return ParserRegistry.get_supported_mimetypes()

    def is_supported(self, mimetype: str) -> bool:
        """Check if a MIME type is supported."""
        return ParserRegistry.is_supported(mimetype)
