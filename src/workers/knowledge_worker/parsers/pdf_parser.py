from __future__ import annotations

import os
import asyncio
import hashlib
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from ..services.chunking import (
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
from agno.media import Image as AgnoImage

from .base_parser import BaseParser
from ..utils.embedding_service import get_embedding_service
from ..utils.content_processor import ContentProcessor
from ..utils.image_summary_agent import image_summary_agent
from ..utils.table_summary_agent import table_summary_agent
from .config import ParserConfig
from src.infra.utils.logger import get_logger
from ..services.progress import publish as publish_progress
from ..services.storage_paths import plan_image_path_for_source

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


class PDFParser(BaseParser):
    """Parser for PDF documents using Docling with advanced image and table processing."""

    def __init__(self, organisation_id: str, user_id: str, storage_backend: Any):
        """Initialize lightweight PDF parser. Heavy deps are provided by services."""
        super().__init__(organisation_id, user_id, storage_backend)
        # Heavy resources (converter, tokenizer, chunker) are obtained lazily from services
        self.embedding_service = get_embedding_service()

    # --------------------------
    # Internal helper utilities
    # --------------------------
    def _extract_tables_pictures(
        self,
        doc_items: List[DocItem],
        dl_doc: Any,
    ) -> Tuple[List[TableItem], List[PictureItem]]:
        """Extract tables and pictures from a list of DocItems.

        Combines direct type detection with label/caption-based linking
        and returns de-duplicated lists.
        """
        # Robust table capture: direct type + label fallback
        tables_direct = [it for it in doc_items if isinstance(it, TableItem)]
        tables_label = [
            it for it in doc_items if getattr(it, "label", None) == DocItemLabel.TABLE
        ]
        tables = list({id(t): t for t in (tables_direct + tables_label)}.values())

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
            except Exception:
                # Best-effort; ignore malformed references
                pass
        pictures_raw = list(
            {id(p): p for p in (pictures_direct + pictures_linked)}.values()
        )
        pictures = self._filter_pictures(pictures_raw, dl_doc)

        return tables, pictures

    def _picture_page_size(
        self, dl_doc: Any, pic: PictureItem
    ) -> Optional[Tuple[float, float]]:
        try:
            provs = getattr(pic, "prov", []) or []
            if not provs:
                return None
            pno = getattr(provs[0], "page_no", None)
            if pno is None:
                return None
            pages = getattr(dl_doc, "pages", []) or []
            if not pages or not (1 <= pno <= len(pages)):
                return None
            page = pages[pno - 1]
            size = getattr(page, "size", None)
            if not size:
                return None
            w = float(getattr(size, "width", 0.0) or 0.0)
            h = float(getattr(size, "height", 0.0) or 0.0)
            return (w, h) if (w > 0 and h > 0) else None
        except Exception:
            return None

    def _is_excluded_by_class(self, pic: PictureItem) -> bool:
        try:
            anns = getattr(pic, "annotations", []) or []
            cls_ann = next(
                (a for a in anns if isinstance(a, PictureClassificationData)), None
            )
            if not cls_ann:
                return False
            predicted = getattr(cls_ann, "predicted_classes", []) or []
            if not predicted:
                return False
            top = predicted[0]
            name = (getattr(top, "class_name", "") or "").lower()
            excluded = {
                PictureClassificationLabel.LOGO.value,
                PictureClassificationLabel.ICON.value,
                PictureClassificationLabel.QR_CODE.value,
                PictureClassificationLabel.BAR_CODE.value,
                PictureClassificationLabel.SIGNATURE.value,
                PictureClassificationLabel.STAMP.value,
            }
            return name in excluded
        except Exception:
            return False

    def _filter_pictures(
        self, pictures: List[PictureItem], dl_doc: Any
    ) -> List[PictureItem]:
        OUT_MIN_AREA_RATIO = 0.004  # ~0.4% of page area

        kept: List[PictureItem] = []
        for p in pictures:
            # 1. Exclude non-body content layers (headers/footers/background/notes)
            try:
                if getattr(p, "content_layer", ContentLayer.BODY) != ContentLayer.BODY:
                    continue
            except Exception:
                pass

            # 2. Exclude decorative classes if available
            if self._is_excluded_by_class(p):
                continue

            # 3. Area ratio filter: drop tiny pictures
            area_ok = True
            try:
                provs = getattr(p, "prov", []) or []
                if provs:
                    bbox = getattr(provs[0], "bbox", None)
                    page_size = self._picture_page_size(dl_doc, p)
                    if bbox is not None and page_size is not None:
                        pw, ph = page_size
                        box_w = float(getattr(bbox, "r", 0.0)) - float(
                            getattr(bbox, "l", 0.0)
                        )
                        box_h = float(getattr(bbox, "b", 0.0)) - float(
                            getattr(bbox, "t", 0.0)
                        )
                        if pw > 0 and ph > 0 and box_w > 0 and box_h > 0:
                            ratio = (box_w * box_h) / (pw * ph)
                            if ratio < OUT_MIN_AREA_RATIO:
                                area_ok = False
            except Exception:
                pass
            if not area_ok:
                continue

            # 4. Keep charts and captioned figures; otherwise require larger area
            try:
                has_caption = False
                if hasattr(p, "caption_text"):
                    try:
                        ct = p.caption_text(dl_doc)
                        has_caption = bool(ct and ct.strip())
                    except Exception:
                        has_caption = False
                is_chart = getattr(p, "label", None) == DocItemLabel.CHART
                if not (has_caption or is_chart):
                    # Recompute area ratio with stricter threshold
                    keep = True
                    provs = getattr(p, "prov", []) or []
                    if provs:
                        bbox = getattr(provs[0], "bbox", None)
                        page_size = self._picture_page_size(dl_doc, p)
                        if bbox is not None and page_size is not None:
                            pw, ph = page_size
                            box_w = float(getattr(bbox, "r", 0.0)) - float(
                                getattr(bbox, "l", 0.0)
                            )
                            box_h = float(getattr(bbox, "b", 0.0)) - float(
                                getattr(bbox, "t", 0.0)
                            )
                            if pw > 0 and ph > 0 and box_w > 0 and box_h > 0:
                                ratio = (box_w * box_h) / (pw * ph)
                                keep = ratio >= (OUT_MIN_AREA_RATIO * 2.0)
                    if not keep:
                        continue
            except Exception:
                pass

            kept.append(p)

        # De-duplicate while preserving order
        seen: set[int] = set()
        out: List[PictureItem] = []
        for p in kept:
            if id(p) in seen:
                continue
            seen.add(id(p))
            out.append(p)
        return out

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

            # Parsing with Docling
            return self._parse_with_docling(
                file_path,
                document_url=document_url,
                source_id=source_id,
                source_blob_path=source_blob_path,
                original_filename=original_filename,
                source_size_bytes=source_size_bytes,
            )

        except Exception as e:
            logger.error(f"Error parsing PDF file {file_path}: {str(e)}")
            return []

    def _parse_with_docling(
        self,
        file_path: str | Path,
        document_url: Optional[str] = None,
        source_id: Optional[str] = None,
        source_blob_path: Optional[str] = None,
        original_filename: Optional[str] = None,
        source_size_bytes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Parse PDF using Docling with advanced structure recognition."""
        file_metadata = ContentProcessor.extract_metadata_from_filename(str(file_path))
        # Prefer caller-provided original filename over temp file's name
        display_filename = (
            original_filename or document_url or file_metadata["filename"]
        )
        logger.info(f"Extracted metadata for PDF: {file_metadata}")

        # Convert document using Docling
        logger.info(f"Converting PDF with Docling: {file_path}")
        result = self._convert_with_fallbacks(file_path)

        # Publish initial progress after extraction step completes
        try:
            publish_progress(
                self.organisation_id,
                source_id,
                status="extracting",
                progress=20,
                updated_at=datetime.utcnow().isoformat(),
            )
        except Exception as _e:  # pragma: no cover - non-critical telemetry
            logger.debug(f"Progress publish failed (extracting 20%): {_e}")
        dl_doc = result.document

        # Chunk document using HybridChunker
        # Obtain heavy resources lazily from services
        if not hasattr(self, "tokenizer") or self.tokenizer is None:
            self.tokenizer = get_tokenizer(EMBED_MODEL_ID)
        if not hasattr(self, "chunker") or self.chunker is None:
            self.chunker = get_hybrid_chunker(EMBED_MODEL_ID)

        raw_chunks = list(self.chunker.chunk(dl_doc=dl_doc))
        if not raw_chunks:
            logger.warning("No chunks produced by HybridChunker")
            return []

        # Collect global signals for document-level metadata
        all_tables: List[TableItem] = []
        all_pictures: List[PictureItem] = []
        all_texts: List[str] = []

        chunk_infos: List[_ChunkInfo] = []
        for index, ch in enumerate(raw_chunks):
            logger.debug(
                f"Processing chunk {index} with {len(getattr(ch.meta, 'doc_items', []) or [])} doc items"
            )
            text = self.chunker.contextualize(ch)
            clean_text = ContentProcessor.clean_text(text)
            # page numbers from doc_items provenance
            page_nos = [
                prov.page_no
                for item in getattr(ch.meta, "doc_items", [])
                for prov in getattr(item, "prov", [])
                if getattr(prov, "page_no", None) is not None
            ]
            page_start = min(page_nos) if page_nos else None
            page_end = max(page_nos) if page_nos else page_start
            logger.debug(f"Chunk {index} spans pages {page_start} to {page_end}")

            doc_items = list(getattr(ch.meta, "doc_items", []) or [])
            logger.debug(f"Chunk {index} has {len(doc_items)} doc items")

            tables, pictures = self._extract_tables_pictures(doc_items, dl_doc)
            logger.debug(f"Chunk {index} has {len(tables)} tables")
            logger.debug(f"Chunk {index} has {len(pictures)} pictures")

            chunk_infos.append(
                _ChunkInfo(
                    index=index,
                    text=clean_text,
                    page_start=page_start,
                    page_end=page_end,
                    has_tables=bool(tables),
                    has_images=bool(pictures),
                    tables=tables,
                    pictures=pictures,
                )
            )
            all_texts.append(clean_text)
            all_tables.extend(tables)
            all_pictures.extend(pictures)

        # Prefer size from OrganisationSource row
        total_pages = max([ci.page_end or 0 for ci in chunk_infos] or [0])

        # Build page indices from provenance to capture items missed in doc_items for some chunks
        tables_by_page: Dict[int, List[TableItem]] = {}
        pictures_by_page: Dict[int, List[PictureItem]] = {}

        def _add_table_to_pages(t: TableItem):
            for prov in getattr(t, "prov", []) or []:
                pno = getattr(prov, "page_no", None)
                if pno is not None:
                    tables_by_page.setdefault(pno, []).append(t)

        def _add_picture_to_pages(p: PictureItem):
            for prov in getattr(p, "prov", []) or []:
                pno = getattr(prov, "page_no", None)
                if pno is not None:
                    pictures_by_page.setdefault(pno, []).append(p)

        for t in all_tables:
            _add_table_to_pages(t)
        for p in all_pictures:
            _add_picture_to_pages(p)
        try:
            for t in getattr(dl_doc, "tables", []) or []:
                _add_table_to_pages(t)
        except Exception:
            pass
        try:
            dl_pictures = getattr(dl_doc, "pictures", []) or []
            dl_pictures = self._filter_pictures(dl_pictures, dl_doc)
            for p in dl_pictures:
                _add_picture_to_pages(p)
        except Exception:
            pass

        file_hash = self._get_file_hash(file_path)
        now_iso = datetime.utcnow().isoformat()
        embedding_service = self.embedding_service
        out: List[Dict[str, Any]] = []

        total_chunks = len(raw_chunks)
        last_progress = 20

        for i, (info, chunk_obj) in enumerate(zip(chunk_infos, raw_chunks)):

            # Reuse precomputed items for this chunk
            tables = list(info.tables)
            pictures = list(info.pictures)

            # Augment with items overlapping this chunk's pages
            pages_for_chunk: List[int] = []
            if info.page_start is not None and info.page_end is not None:
                pages_for_chunk = list(range(info.page_start, info.page_end + 1))
            elif info.page_start is not None:
                pages_for_chunk = [info.page_start]
            table_ids = {id(x) for x in tables}
            picture_ids = {id(x) for x in pictures}
            for pno in pages_for_chunk:
                for t in tables_by_page.get(pno, []) or []:
                    if id(t) not in table_ids:
                        tables.append(t)
                        table_ids.add(id(t))
                for p in pictures_by_page.get(pno, []) or []:
                    if id(p) not in picture_ids:
                        pictures.append(p)
                        picture_ids.add(id(p))

            # Summarize tables (best-effort)
            table_summaries: List[str] = []
            for t in tables[:3]:  # bound table summaries per chunk
                try:
                    md = t.export_to_markdown(doc=dl_doc)
                    resp = table_summary_agent.run(message=md)
                    table_summaries.append(getattr(resp, "content", "") or "")
                except Exception as e:  # pragma: no cover - best effort
                    logger.debug(f"Table summary failed: {e}")

            # Summarize images and upload (best-effort)
            image_summaries: List[str] = []
            image_metadata: List[Dict[str, Any]] = []
            if pictures:
                image_summaries, image_metadata = self._process_images(
                    pictures[:5],
                    result,
                    page_hint=info.page_start,
                    source_id=source_id,
                    source_blob_path=source_blob_path,
                )

            # Build final content for embedding
            parts: List[str] = []
            if info.text:
                parts.append(info.text)
            if table_summaries:
                parts.append("\n\nTables:\n" + "\n---\n".join(table_summaries))
            if image_summaries:
                parts.append("\n\nImages:\n" + "\n---\n".join(image_summaries))
            combined_content = "".join(parts).strip()

            # Skip trivial chunks
            if not ContentProcessor.validate_content(combined_content):
                logger.debug(f"Skipping trivial chunk index={info.index}")
                continue

            embedding = embedding_service.get_embedding(combined_content)
            content_hash = hashlib.sha256(combined_content.encode("utf-8")).hexdigest()

            # Basic text and content stats
            char_count = len(info.text or "")
            line_count = (info.text or "").count("\n") + (1 if info.text else 0)
            try:
                token_count = (
                    len(self.tokenizer.encode(info.text, add_special_tokens=False))
                    if info.text
                    else 0
                )
            except Exception:
                token_count = None

            meta: Dict[str, Any] = {
                "chunk_index": info.index,
                "page_start": info.page_start,
                "page_end": info.page_end,
                "contains_table": bool(tables),
                "contains_image": bool(pictures),
                "table_count": len(tables),
                "image_count": len(pictures),
                "char_count": char_count,
                "line_count": line_count,
                "token_count": token_count,
                "user_id": str(self.user_id),
                "origin": {
                    "mimetype": "application/pdf",
                    "filename": display_filename,
                    "blob_path": source_blob_path,
                },
                "created_at": now_iso,
                "images": image_metadata,
                "source_id": source_id,
                "total_pages": total_pages,
                **file_metadata,
            }

            out.append(
                {
                    "id": hashlib.sha256(
                        f"{file_hash}:{info.index}".encode()
                    ).hexdigest(),
                    "name": display_filename,
                    "meta_data": meta,
                    "filters": {
                        "has_table": bool(tables),
                        "has_image": bool(pictures),
                        "page_start": info.page_start,
                        "source_id": source_id,
                    },
                    "content": combined_content,
                    "embedding": embedding,
                    "usage": {},
                    "content_hash": content_hash,
                }
            )

            # Incremental progress update during chunking (20% -> up to 95%)
            try:
                if source_id and total_chunks > 0:
                    # Scale chunking progress across 20..95
                    p = 20 + int(75 * (i + 1) / total_chunks)
                    if p > 95:
                        p = 95
                    if p > last_progress:
                        last_progress = p
                        publish_progress(
                            self.organisation_id,
                            source_id,
                            status="extracting",
                            progress=p,
                            updated_at=datetime.utcnow().isoformat(),
                        )
            except Exception as _e:  # pragma: no cover
                logger.debug(
                    f"Progress publish failed (chunk {i+1}/{total_chunks}): {_e}"
                )

        logger.info("Built %d enriched PDF chunks", len(out))
        return out

    # Conversion with graceful fallbacks for low-memory environments (e.g., WinError 1455)
    def _convert_with_fallbacks(self, file_path: str | Path):
        try:
            converter = get_pdf_converter()
            return converter.convert(file_path)
        except Exception as e:
            msg = str(e).lower()
            is_memory_pressure = (
                getattr(e, "winerror", None) == 1455
                or "paging file is too small" in msg
                or "os error 1455" in msg
                or isinstance(e, MemoryError)
            )
            if not is_memory_pressure:
                raise
            logger.warning(
                "Docling convert failed due to memory pressure; retrying with lightweight options"
            )

            # Build a lightweight pipeline configuration
            try:
                light_converter = get_light_pdf_converter()
                return light_converter.convert(file_path)
            except Exception as e2:
                logger.error(f"Lightweight convert also failed: {e2}")
                raise

    # ------------------------------------------------------------------
    # Legacy feature helper methods adapted from previous implementation
    # ------------------------------------------------------------------
    def _process_images(
        self,
        pictures: List[PictureItem],
        result: Any,
        page_hint: Optional[int],
        source_id: Optional[str],
        source_blob_path: Optional[str],
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        summaries: List[str] = []
        metadata: List[Dict[str, Any]] = []
        for idx, pic in enumerate(pictures):
            try:
                # Prefer exact page number from picture provenance, else fallback to page_hint
                page_no = None
                try:
                    for prov in getattr(pic, "prov", []) or []:
                        pno = getattr(prov, "page_no", None)
                        if pno is not None:
                            page_no = pno
                            break
                except Exception:
                    page_no = None
                if page_no is None:
                    page_no = page_hint
                pil_img = pic.get_image(result.document)
                buf = BytesIO()
                pil_img.save(buf, format="PNG")
                img_bytes = buf.getvalue()
                # Upload to Azure blob storage (best-effort)
                blob_path = self._upload_image_bytes_sync(
                    img_bytes,
                    page_no,
                    idx,
                    source_id=source_id,
                    source_blob_path=source_blob_path,
                )
                resp = image_summary_agent.run(
                    message="Describe the image for retrieval",
                    images=[AgnoImage(content=img_bytes)],
                )
                summaries.append(getattr(resp, "content", "") or "")
                width, height = pil_img.size
                metadata.append(
                    {
                        "blob_path": blob_path,
                        "index": idx,
                        "page_number": page_no,
                        "width": width,
                        "height": height,
                        "mode": pil_img.mode,
                        "bytes": len(img_bytes),
                    }
                )
            except Exception as e:  # pragma: no cover
                logger.debug(f"Image processing failed: {e}")
        return summaries, metadata

    def _upload_image_bytes_sync(
        self,
        image_bytes: bytes,
        page_number: Optional[int],
        idx: int,
        source_id: Optional[str],
        source_blob_path: Optional[str],
    ) -> str:
        """Synchronous wrapper to upload image bytes to Azure blob storage.

        Returns a blob URL or an empty string on failure.
        """
        if not self.storage_backend:
            return ""
        # New deterministic knowledgebase path grouped by user and source
        # If we have the original document blob path, derive images folder right next to it.
        blob_path = plan_image_path_for_source(
            user_id=str(self.user_id),
            source_id=source_id,
            source_blob_path=source_blob_path,
            page_number=page_number,
            index=idx,
        )

        async def _upload():
            try:
                blob_service = await self.storage_backend._get_blob_service_client()  # type: ignore[attr-defined]
                blob_client = blob_service.get_blob_client(self.storage_backend.container, blob_path)  # type: ignore[attr-defined]
                from azure.storage.blob import (
                    ContentSettings,
                )  # local import to avoid dependency at module import

                await blob_client.upload_blob(
                    image_bytes,
                    overwrite=True,
                    content_settings=ContentSettings(content_type="image/png"),
                )
                return blob_path
            except Exception as e:  # pragma: no cover
                logger.debug(f"Image upload failed: {e}")
                # Even on failure, return the planned path so metadata is populated
                return blob_path

        try:
            return asyncio.run(_upload())
        except RuntimeError:
            # Event loop already running (e.g., inside async context) -> schedule differently
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(_upload())  # type: ignore

    # Removed document classification to avoid incorrect labeling and reduce noise

    def _get_file_hash(self, file_path: str | Path) -> str:
        """Generate SHA256 hash of the file."""
        try:
            with open(file_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.error(f"Error generating file hash: {str(e)}")
            return "unknown_hash"
