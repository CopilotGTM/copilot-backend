"""Table processor - handles table extraction, summarization, and image storage."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.agents.table_summary_agent import table_summary_agent
from src.workers.knowledge_worker.services.storage_paths import plan_table_image_path_for_source

if TYPE_CHECKING:
    from docling_core.types.doc import TableItem

    from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
    from src.workers.knowledge_worker.models import ProcessingContext

logger = get_logger("table_processor")


class TableProcessor:
    """
    Handles table extraction, LLM summarization, and image storage.

    Extracted from PDFParser.summarize_tables() to:
    1. Separate concerns (parsing vs processing)
    2. Make table processing reusable across parsers
    3. Enable easier testing and mocking

    Usage:
        processor = TableProcessor(storage, loop)
        summaries, images = processor.process_tables(tables, dl_doc, context)
    """

    def __init__(
        self,
        storage: AzureOrganisationBlobStorage | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        """
        Initialize the table processor.

        Args:
            storage: Azure blob storage client for uploading table images.
                    If None, images won't be stored.
            loop: Event loop for running async operations.
                 If None, will create a new loop when needed.
        """
        self._storage = storage
        self._loop = loop

    def process_tables(
        self,
        tables: list[TableItem],
        dl_doc: Any,
        context: ProcessingContext,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """
        Process tables: generate LLM summaries and store images.

        This method was extracted from PDFParser.summarize_tables() (lines 240-317).

        Args:
            tables: List of Docling TableItem objects
            dl_doc: The Docling document object (needed for image extraction)
            context: Processing context with org/source/user info

        Returns:
            Tuple of:
            - summaries: List of LLM-generated table summaries
            - image_metadata: List of dicts with {path, mimetype} for stored images
        """
        if not tables:
            return [], []

        logger.info(f"Processing {len(tables)} tables")
        summaries: list[str] = []
        table_images: list[dict[str, Any]] = []

        for idx, table in enumerate(tables):
            try:
                # Generate summary from table markdown
                summary = self._generate_summary(table, dl_doc, idx)
                if summary:
                    summaries.append(summary)

                # Store table as image
                image_meta = self._store_table_image(table, dl_doc, context, idx)
                if image_meta:
                    table_images.append(image_meta)

            except Exception as e:
                logger.error(f"Error processing table {idx}: {e}")
                continue

        logger.info(f"Generated {len(summaries)} summaries and {len(table_images)} table images")
        return summaries, table_images

    def _generate_summary(
        self,
        table: TableItem,
        dl_doc: Any,
        idx: int,
    ) -> str | None:
        """
        Generate LLM summary for a table.

        Args:
            table: The Docling TableItem
            dl_doc: The Docling document
            idx: Table index (for logging)

        Returns:
            Summary string or None if generation failed
        """
        try:
            # Check if this object actually has export_to_markdown method
            # (DocItem with TABLE label vs actual TableItem)
            if not hasattr(table, "export_to_markdown"):
                logger.debug(
                    f"Table {idx} does not support markdown export (DocItem, not TableItem)"
                )
                return None

            md_content = table.export_to_markdown(doc=dl_doc)
            if not md_content or not md_content.strip():
                logger.debug(f"Table {idx} has no markdown content")
                return None

            resp = table_summary_agent.run(message=md_content)
            return resp.content

        except Exception as e:
            logger.warning(f"Failed to generate summary for table {idx}: {e}")
            return None

    def _store_table_image(
        self,
        table: TableItem,
        dl_doc: Any,
        context: ProcessingContext,
        idx: int,
    ) -> dict[str, Any] | None:
        """
        Extract and store table as PNG image.

        Args:
            table: The Docling TableItem
            dl_doc: The Docling document
            context: Processing context
            idx: Table index

        Returns:
            Dict with {path, mimetype} or None if storage failed
        """
        if not self._storage:
            return None

        try:
            # Check if this object actually has get_image method
            # (DocItem with TABLE label vs actual TableItem)
            if not hasattr(table, "get_image"):
                logger.debug(
                    f"Table {idx} does not support image extraction (DocItem, not TableItem)"
                )
                return None

            # Get table image from Docling
            table_img = table.get_image(dl_doc)
            if not table_img:
                return None

            # Convert to PNG bytes
            buf = io.BytesIO()
            table_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            if not img_bytes:
                return None

            # Get page number for path planning
            table_pages = [getattr(prov, "page_no", None) for prov in getattr(table, "prov", [])]
            first_page = min([p for p in table_pages if p is not None], default=None)

            # Plan blob path
            blob_path = plan_table_image_path_for_source(
                user_id=context.user_id,
                source_id=context.source_id,
                source_blob_path=context.source_blob_path,
                page_no=first_page,
                table_idx=idx,
            )

            # Upload to blob storage using the event loop
            loop = self._loop or asyncio.new_event_loop()
            loop.run_until_complete(
                self._storage.upload_bytes(blob_path, img_bytes, content_type="image/png")
            )

            return {
                "path": blob_path,
                "mimetype": "image/png",
            }

        except Exception as e:
            logger.warning(f"Failed to store image for table {idx}: {e}")
            return None
