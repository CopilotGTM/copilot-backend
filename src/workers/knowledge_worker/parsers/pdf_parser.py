from __future__ import annotations

import os
import asyncio
import hashlib
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
import json
import gzip
import hashlib as _hashlib
from dataclasses import dataclass
from PIL import Image

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None
from src.workers.knowledge_worker.services.document_converter import get_doc_converter
from src.workers.knowledge_worker.services.chunking import (
    get_pdf_converter,
    get_light_pdf_converter,
    get_hybrid_chunker,
    get_tokenizer,
    DEFAULT_EMBED_MODEL_ID,
)
from docling_core.types.doc import (
    TableItem,
    PictureItem,
    DocItemLabel,
    DocItem,
    ContentLayer,
    PictureClassificationData,
    PictureClassificationLabel,
)
from docling_core.types.doc.document import DoclingDocument
from agno.media import Image as AgnoImage
from datetime import datetime, timezone
from .base_parser import BaseParser
from src.workers.knowledge_worker.agents.embedding_service import get_embedding_service
from src.workers.knowledge_worker.utils.content_processor import ContentProcessor
from src.workers.knowledge_worker.agents.image_summary_agent import image_summary_agent
from src.workers.knowledge_worker.agents.table_summary_agent import table_summary_agent
from src.workers.knowledge_worker.config import KnowledgeWorkerConfig
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.services.progress import ProgressPublisher
from src.workers.knowledge_worker.services.storage_paths import (
    plan_image_path_for_source,
    plan_table_image_path_for_source,
)
from src.domain.models.organisation_tables.knowledge_models import OrganisationDocument
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSourceStatus,
)

logger = get_logger("pdf_parser")
EMBED_MODEL_ID = DEFAULT_EMBED_MODEL_ID


@dataclass
class _ChunkInfo:
    index: int
    text: str
    page_start: Optional[int]
    page_end: Optional[int]
    has_tables: bool
    has_images: bool
    tables: List[TableItem]
    pictures: List[PictureItem]
    table_summaries: List[str]
    image_summaries: List[str]
    table_images: List[Dict[str, Any]]  # Table image metadata and bytes
    picture_images: List[Dict[str, Any]]  # Picture image metadata and bytes
    enhanced_content: str  # Combined text + summaries for embedding


class PDFParser(BaseParser):
    """Parser for PDF documents using Docling with advanced image and table processing."""

    def __init__(self, organisation_id: str, user_id: str, storage_backend: Any):
        """Initialize lightweight PDF parser. Heavy deps are provided by services."""
        super().__init__(organisation_id, user_id, storage_backend)
        # Heavy resources (converter, tokenizer, chunker) are obtained lazily from services
        self.embedding_service = get_embedding_service()
        self.tokenizer = get_tokenizer(EMBED_MODEL_ID)
        self.chunker = get_hybrid_chunker(EMBED_MODEL_ID)

    @classmethod
    def supported_mimetypes(cls) -> List[str]:
        return ["application/pdf"]

    def parse(
        self,
        file_path: str | Path,
        document_url: Optional[str] = None,
        source_id: Optional[str] = None,
        source_blob_path: Optional[str] = None,
        original_filename: Optional[str] = None,
        source_size_bytes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Parse a PDF file and return processed document data using Docling.

        Args:
            file_path: Path to the PDF file
            document_url: Optional URL of the document

        Returns:
            List of processed document chunks/pages with enhanced content extraction
        """
        try:
            logger.info(f"Starting PDF parsing with Docling for file: {file_path}")
            self.publisher = ProgressPublisher(
                organisation_id=self.organisation_id,
                source_id=source_id,
            )

            # Local path of downloaded file (should exist)
            temp_file_path = file_path
            if not os.path.isfile(temp_file_path):
                logger.error(f"File not found: {temp_file_path}")
                return []

            # Commented for testing only
            doc_converter = get_doc_converter()
            result = doc_converter.convert(temp_file_path)
            self.publisher.publish(
                status=OrganisationSourceStatus.EXTRACTING, progress=20
            )

            # # Docling document object
            dl_doc = result.document

            # dl_doc = _load_docling_from_cache("C:\\Users\\nikhi\\Downloads\\sample-1.1.pdf") # Testing only

            # Getting raw chunks from chunker
            raw_chunks = list(self.chunker.chunk(dl_doc=dl_doc))
            total_chunks = len(raw_chunks)
            logger.info(f"Total chunks produced by HybridChunker: {total_chunks}")
            if not raw_chunks:
                logger.warning("No chunks produced by HybridChunker")
                return []

            output: List[OrganisationDocument] = []
            for index, chunk in enumerate(raw_chunks):
                logger.info(f"Chunk {index} type: {type(chunk)}")
                chunk_info = self._chunk_level_processing(
                    index, chunk, dl_doc, source_id, source_blob_path
                )

                # Store multimodal content in meta_data for retrieval
                chunk_meta_data = {
                    "source_metadata": {
                        "document_url": document_url,
                        "path": source_blob_path,
                    },
                    "chunk_metadata": {
                        "page_start": chunk_info.page_start,
                        "page_end": chunk_info.page_end,
                    },
                    "table_images_list": chunk_info.table_images,
                    "images_list": chunk_info.picture_images,
                }

                chunk_filters = {
                    "has_tables": chunk_info.has_tables,
                    "has_images": chunk_info.has_images,
                    "is_pdf": True,
                }
                chunk_hash = self._compute_chunk_hash(chunk_info)
                embedding = self.embedding_service.get_embedding(
                    chunk_info.enhanced_content
                )
                output.append(
                    OrganisationDocument(
                        source_id=source_id,
                        uploader_user_id=UUID(self.user_id),
                        name=original_filename,
                        content=chunk_info.enhanced_content,
                        embedding=embedding,
                        meta_data=chunk_meta_data,
                        filters=chunk_filters,
                        content_hash=chunk_hash,
                        is_org_wide=True,
                        is_searchable=True,
                        is_archived=False,
                    )
                )
                self.publisher.publish(
                    status=OrganisationSourceStatus.EXTRACTING,
                    progress=int((index + 1) / total_chunks * 100),
                )

            return output

        except Exception as e:
            logger.error(f"Error parsing PDF file {file_path}: {str(e)}")
            return []

    def _chunk_level_processing(
        self, index, chunk_obj, dl_doc, source_id=None, source_blob_path=None
    ):
        """Process individual chunk: clean text, extract page numbers, tables, images."""
        text = self.chunker.contextualize(chunk_obj)
        clean_text = ContentProcessor.clean_text(text)
        page_start, page_end = self._extract_page_numbers(chunk_obj)

        # Extract tables and images separately
        doc_items = list(getattr(chunk_obj.meta, "doc_items", []) or [])
        logger.info(f"Chunk {index} doc_items count: {len(doc_items)}")
        logger.info(f"Chunk {index} pages: {page_start}-{page_end}")

        # Extract tables and their summaries/images
        tables = self._extract_tables(doc_items, dl_doc)
        table_summaries, table_images = (
            self.summarize_tables(tables, dl_doc, source_id, source_blob_path)
            if tables
            else ([], [])
        )

        # Extract pictures and their summaries
        pictures = self._extract_pictures(doc_items, dl_doc)
        image_summaries, picture_images = (
            self.summarize_pictures(pictures, dl_doc, source_id, source_blob_path)
            if pictures
            else ([], [])
        )

        # Process tables for both summaries and images in one step

        # Create enhanced content for better embeddings
        enhanced_content = self._create_enhanced_content(
            clean_text, table_summaries, image_summaries
        )
        logger.info(f"Enhanced content for chunk {index}: {enhanced_content}")
        output = _ChunkInfo(
            index=index,
            text=clean_text,
            page_start=page_start,
            page_end=page_end,
            has_tables=bool(tables),
            has_images=bool(pictures),
            tables=tables,
            pictures=pictures,
            table_summaries=table_summaries,
            image_summaries=image_summaries,
            table_images=table_images,
            picture_images=picture_images,
            enhanced_content=enhanced_content,
        )
        return output

    def summarize_tables(
        self,
        tables: List[TableItem],
        dl_doc: Any,
        source_id: Optional[str] = None,
        source_blob_path: Optional[str] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Generate summaries for tables and extract their images simultaneously."""
        if not tables:
            return [], []

        logger.info(f"Processing {len(tables)} tables")
        summaries: List[str] = []
        table_images: List[Dict[str, Any]] = []

        for idx, table in enumerate(tables):
            try:
                # Get table markdown for summary
                try:
                    md_content = table.export_to_markdown(doc=dl_doc)
                    if md_content and md_content.strip():
                        resp = table_summary_agent.run(message=md_content)
                        summaries.append(resp.content)
                except Exception:
                    logger.warning(f"Failed to generate summary for table {idx}")

                # Extract and store table image
                try:
                    table_img = table.get_image(dl_doc)
                    if table_img and self.storage_backend:
                        # Convert to bytes
                        import io

                        buf = io.BytesIO()
                        table_img.save(buf, format="PNG")
                        img_bytes = buf.getvalue()

                        # Get page info and plan storage path
                        table_pages = [
                            getattr(prov, "page_no", None)
                            for prov in getattr(table, "prov", [])
                        ]
                        first_page = min(
                            [p for p in table_pages if p is not None], default=None
                        )

                        blob_path = plan_table_image_path_for_source(
                            user_id=self.user_id,
                            source_id=source_id,
                            source_blob_path=source_blob_path,
                            page_no=first_page,
                            table_idx=idx,
                        )

                        # Store image and metadata
                        import asyncio

                        loop = asyncio.get_event_loop()
                        loop.run_until_complete(
                            self.storage_backend.upload_bytes(
                                blob_path, img_bytes, content_type="image/png"
                            )
                        )

                        table_images.append(
                            {
                                "path": blob_path,
                                "mimetype": "image/png",
                            }
                        )

                except Exception:
                    logger.warning(f"Failed to extract image for table {idx}")

            except Exception as e:
                logger.error(f"Error processing table {idx}: {e}")
                continue

        logger.info(
            f"Generated {len(summaries)} summaries and {len(table_images)} images"
        )
        return summaries, table_images

    def summarize_pictures(
        self,
        pictures: List[PictureItem],
        dl_doc: Any,
        source_id: Optional[str] = None,
        source_blob_path: Optional[str] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Generate summaries for pictures and store images simultaneously."""
        if not pictures:
            return [], []

        logger.info(f"Processing {len(pictures)} pictures")
        summaries: List[str] = []
        picture_images: List[Dict[str, Any]] = []

        for idx, pic in enumerate(pictures):
            try:
                # Get PIL image and convert to bytes
                pil_img = pic.get_image(dl_doc)
                if not pil_img:
                    continue

                import io

                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                img_bytes = buf.getvalue()

                if not img_bytes:
                    continue

                # Generate summary using image agent
                ag_img = AgnoImage(content=img_bytes)
                resp = image_summary_agent.run(
                    message="Analyze and summarize this image for RAG purposes",
                    images=[ag_img],
                )
                content = getattr(resp, "content", None) or str(resp)

                # Filter out generic responses
                if content and not any(
                    marker in content.lower()
                    for marker in [
                        "provide the image",
                        "describe it",
                        "sufficient detail",
                    ]
                ):
                    summaries.append(content)

                # Store image to blob storage
                try:
                    if self.storage_backend:
                        # Get page info and plan storage path
                        pic_pages = [
                            getattr(prov, "page_no", None)
                            for prov in getattr(pic, "prov", [])
                        ]
                        first_page = min(
                            [p for p in pic_pages if p is not None], default=None
                        )

                        blob_path = plan_image_path_for_source(
                            user_id=self.user_id,
                            source_id=source_id,
                            source_blob_path=source_blob_path,
                            page_no=first_page,
                            idx=f"pic_{idx}",
                        )

                        # Store image
                        import asyncio

                        loop = asyncio.get_event_loop()
                        loop.run_until_complete(
                            self.storage_backend.upload_bytes(
                                blob_path, img_bytes, content_type="image/png"
                            )
                        )

                        picture_images.append(
                            {
                                "path": blob_path,
                                "mimetype": "image/png",
                            }
                        )

                except Exception:
                    logger.warning(f"Failed to store image for picture {idx}")

            except Exception:
                logger.warning(f"Failed to process picture {idx}")
                continue

        logger.info(
            f"Generated {len(summaries)} summaries and {len(picture_images)} images"
        )
        return summaries, picture_images

    def _create_enhanced_content(
        self, text: str, table_summaries: List[str], image_summaries: List[str]
    ) -> str:
        """Create enhanced content by combining text with table and image summaries.

        This creates a richer context for embedding generation that captures
        multimodal document information.
        """
        sections = [text.strip()]

        if table_summaries:
            table_context = "\n\n[TABLE CONTENT]\n" + "\n".join(
                f"Table {i+1}: {summary}" for i, summary in enumerate(table_summaries)
            )
            sections.append(table_context)

        if image_summaries:
            image_context = "\n\n[VISUAL CONTENT]\n" + "\n".join(
                f"Image {i+1}: {summary}" for i, summary in enumerate(image_summaries)
            )
            sections.append(image_context)

        return "\n".join(sections)

    def _compute_chunk_hash(self, chunk_info: _ChunkInfo) -> str:
        """Compute a SHA-256 hash for the chunk content to detect duplicates.

        The hash is based on the enhanced content (text + summaries) to ensure
        that chunks with identical multimodal content are properly deduplicated.
        """
        # Use the enhanced content which includes text + table summaries + image summaries
        content_to_hash = chunk_info.enhanced_content

        # Add metadata that affects content uniqueness
        hash_components = [
            content_to_hash,
            str(chunk_info.has_tables),
            str(chunk_info.has_images),
            str(len(chunk_info.table_summaries)),
            str(len(chunk_info.image_summaries)),
        ]

        # Create a deterministic string for hashing
        hash_input = "|".join(hash_components)

        # Generate SHA-256 hash
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def _is_excluded_by_class(self, pic: PictureItem) -> bool:
        """Exclude decorative/irrelevant pictures based on classification label, if present."""
        try:
            data = getattr(pic, "classification", None)
            label = getattr(data, "label", None)
            if label is None:
                return False
            # Handle both Enum and str
            label_str = str(label).lower()
            excluded = {
                "logo",
                "icon",
                "background",
                "decoration",
                "decorative",
            }
            return any(tok in label_str for tok in excluded)
        except Exception:
            return False

    def _filter_pictures(
        self, pictures: List[PictureItem], dl_doc: Any
    ) -> List[PictureItem]:
        """Filter out tiny/irrelevant pictures in a best-effort manner.

        - drop pictures classified as decorative (logo/icon/background)
        - drop pictures with very small raster size
        """
        out: List[PictureItem] = []
        for p in pictures:
            try:
                if self._is_excluded_by_class(p):
                    continue
                keep = True
                try:
                    img = p.get_image(dl_doc)
                    w, h = img.size
                    if w < 32 or h < 32 or (w * h) < (32 * 32):
                        keep = False
                except Exception:
                    # If we cannot materialize the image, keep it (best-effort)
                    keep = True
                if keep:
                    out.append(p)
            except Exception:
                # Ignore malformed entries
                pass
        return out

    def _extract_page_numbers(self, chunk_obj) -> int:
        """Extract page numbers from chunk metadata, if available."""
        page_nos: List[int] = []
        seen_pages = set()
        meta = getattr(chunk_obj, "meta", None)
        doc_items = getattr(meta, "doc_items", []) if meta else []
        for item in doc_items:
            for prov in getattr(item, "prov", []) or []:
                page_no = getattr(prov, "page_no", None)
                if page_no is not None and page_no not in seen_pages:
                    seen_pages.add(page_no)
                    page_nos.append(page_no)
        page_start = min(page_nos) if page_nos else None
        page_end = max(page_nos) if page_nos else page_start
        return page_start, page_end

    def _extract_tables(self, doc_items: List[DocItem], dl_doc: Any) -> List[TableItem]:
        """Extract tables from this chunk's doc_items.

        The chunker creates DocItem objects with label='table' instead of TableItem objects.
        We need to map these back to the original TableItem objects from the document.
        """
        if not doc_items:
            return []

        logger.info(f"🔍 _extract_tables: Analyzing {len(doc_items)} doc_items...")

        # Extract direct TableItem instances
        direct_tables = [item for item in doc_items if isinstance(item, TableItem)]

        # Map DocItem objects with label='table' to TableItem objects from document
        mapped_tables = []
        for item in doc_items:
            item_label = getattr(item, "label", None)
            is_table_labeled = (
                str(item_label).lower() == "table" if item_label else False
            )

            if not isinstance(item, TableItem) and is_table_labeled:
                # Get pages for this DocItem
                item_pages = [
                    getattr(prov, "page_no", None) for prov in getattr(item, "prov", [])
                ]
                item_pages = [p for p in item_pages if p is not None]

                logger.info(
                    f"🔄 Mapping DocItem with label='table' on pages {item_pages}"
                )

                # Find matching TableItem in document by page overlap
                for doc_table in dl_doc.tables:
                    doc_table_pages = [
                        getattr(prov, "page_no", None)
                        for prov in getattr(doc_table, "prov", [])
                    ]
                    doc_table_pages = [p for p in doc_table_pages if p is not None]

                    if set(item_pages) & set(doc_table_pages):
                        logger.info(
                            f"✅ Found matching TableItem for pages {item_pages}"
                        )
                        mapped_tables.append(doc_table)
                        break

        # Combine and deduplicate
        unique_tables = list(
            {id(table): table for table in direct_tables + mapped_tables}.values()
        )

        logger.info(
            f"📊 Extracted {len(unique_tables)} TableItem(s): {len(direct_tables)} direct + {len(mapped_tables)} mapped"
        )

        if not unique_tables and doc_items:
            item_labels = [getattr(item, "label", None) for item in doc_items]
            logger.info(f"❌ No tables found. Labels: {item_labels}")

        return unique_tables

    def _extract_pictures(
        self, doc_items: List[DocItem], dl_doc: Any
    ) -> List[PictureItem]:
        """Extract pictures from document items."""
        logger.info(f"Extracting pictures from {len(doc_items)} doc_items")

        # Robust picture capture: direct + caption-linked
        pictures_direct = [it for it in doc_items if isinstance(it, PictureItem)]
        pictures_linked: List[PictureItem] = []
        for it in doc_items:
            try:
                if getattr(it, "label", None) == DocItemLabel.CAPTION and hasattr(
                    it, "parent"
                ):
                    cref = getattr(it.parent, "cref", "")
                    if isinstance(cref, str) and cref.startswith("#/pictures/"):
                        idx = int(cref.rsplit("/", 1)[1])
                        pic = dl_doc.pictures[idx]
                        pictures_linked.append(pic)
            except Exception as e:
                # Best-effort; ignore malformed references
                logger.warning(f"Error processing caption reference: {e}")
                continue
        pictures_raw = list(
            {id(p): p for p in (pictures_direct + pictures_linked)}.values()
        )

        logger.info(
            f"Found {len(pictures_direct)} direct PictureItems, {len(pictures_linked)} caption-linked pictures"
        )

        pictures = self._filter_pictures(pictures_raw, dl_doc)

        logger.info(f"Final picture count: {len(pictures)} pictures (after filtering)")
        return pictures


# # # ------------------------------ Cache Helpers ------------------------------ #

# _CACHE_DIR = Path("cache/docling")

# def _file_hash(path: str | Path) -> str:
#     h = _hashlib.sha256()
#     with open(path, "rb") as f:
#         for blk in iter(lambda: f.read(65536), b""):
#             h.update(blk)
#     return h.hexdigest()


# def _cache_paths(h: str) -> tuple[Path, Path]:
#     return (_CACHE_DIR / f"{h}.json.gz", _CACHE_DIR / f"{h}.meta.json")


# def _load_docling_from_cache(file_path: str | Path) -> Optional[DoclingDocument]:
#     """Load a previously cached DoclingDocument. Returns None if missing."""
#     _CACHE_DIR.mkdir(parents=True, exist_ok=True)
#     h = _file_hash(file_path)
#     doc_path, _meta_path = _cache_paths(h)
#     if not doc_path.exists():
#         return None
#     with gzip.open(doc_path, "rt", encoding="utf-8") as f:
#         data = json.load(f)
#     return DoclingDocument(**data)


# def _save_docling_to_cache(file_path: str | Path, dl_doc: DoclingDocument, extra_meta: Optional[Dict[str, Any]] = None) -> None:
#     """(Not used in fast path) Save DoclingDocument to cache for future runs."""
#     _CACHE_DIR.mkdir(parents=True, exist_ok=True)
#     h = _file_hash(file_path)
#     doc_path, meta_path = _cache_paths(h)
#     if doc_path.exists():  # don't overwrite
#         return
#     payload = dl_doc.model_dump(mode="json")
#     with gzip.open(doc_path, "wt", encoding="utf-8") as f:
#         json.dump(payload, f)
#     meta = {"version": 1, "pages": len(dl_doc.pages)}
#     if extra_meta:
#         meta.update(extra_meta)
#     with open(meta_path, "w", encoding="utf-8") as mf:
#         json.dump(meta, mf)


# # ------------------------------ Dev Main ---------------------------------- #

# def main():

#     parser = PDFParser(organisation_id="dev-org", user_id="dev-user", storage_backend=None)
#     parser.parse("C:\\Users\\nikhi\\Downloads\\sample-1.1.pdf")
#     print("Done (fast path).")


# if __name__ == "__main__":  # pragma: no cover
#     main()
