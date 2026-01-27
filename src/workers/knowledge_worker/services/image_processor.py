"""Image processor - handles image extraction, filtering, summarization, and storage."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from agno.media import Image as AgnoImage

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.agents.image_summary_agent import image_summary_agent
from src.workers.knowledge_worker.services.storage_paths import plan_image_path_for_source

if TYPE_CHECKING:
    from docling_core.types.doc import PictureItem

    from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
    from src.workers.knowledge_worker.models import ProcessingContext

logger = get_logger("image_processor")

# Markers that indicate the LLM couldn't process the image
GENERIC_RESPONSE_MARKERS = [
    "provide the image",
    "describe it",
    "sufficient detail",
    "cannot analyze",
    "unable to process",
]

# Minimum image dimensions to keep (filter out icons/decorations)
MIN_IMAGE_WIDTH = 32
MIN_IMAGE_HEIGHT = 32
MIN_IMAGE_AREA = 32 * 32

# Classification labels that indicate decorative images
EXCLUDED_CLASSIFICATIONS = {
    "logo",
    "icon",
    "background",
    "decoration",
    "decorative",
}


class ImageProcessor:
    """
    Handles image extraction, filtering, LLM summarization, and storage.

    Extracted from PDFParser.summarize_pictures() and _filter_pictures() to:
    1. Separate concerns (parsing vs processing)
    2. Make image processing reusable across parsers
    3. Enable easier testing and mocking

    Usage:
        processor = ImageProcessor(storage, loop)
        summaries, images = processor.process_images(pictures, dl_doc, context)
    """

    def __init__(
        self,
        storage: AzureOrganisationBlobStorage | None = None,
    ):
        """
        Initialize the image processor.

        Uses a persistent thread-local event loop via run_async() to prevent
        memory leaks from creating new event loops per task.

        Args:
            storage: Azure blob storage client for uploading images.
                    If None, images won't be stored.
        """
        self._storage = storage

    def process_images(
        self,
        pictures: list[PictureItem],
        dl_doc: Any,
        context: ProcessingContext,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """
        Process images: filter, generate LLM summaries, and store.

        This method combines logic from:
        - PDFParser.summarize_pictures() (lines 319-399)
        - PDFParser._filter_pictures() (lines 480-505)

        Args:
            pictures: List of Docling PictureItem objects
            dl_doc: The Docling document object (needed for image extraction)
            context: Processing context with org/source/user info

        Returns:
            Tuple of:
            - summaries: List of LLM-generated image summaries
            - image_metadata: List of dicts with {path, mimetype} for stored images
        """
        if not pictures:
            return [], []

        # Filter out small/decorative images first
        filtered = self.filter_pictures(pictures, dl_doc)
        logger.info(f"Processing {len(filtered)} images (filtered from {len(pictures)})")

        if not filtered:
            return [], []

        summaries: list[str] = []
        picture_images: list[dict[str, Any]] = []

        for idx, pic in enumerate(filtered):
            try:
                # Extract image bytes
                img_bytes = self._extract_image_bytes(pic, dl_doc)
                if not img_bytes:
                    continue

                # Generate summary
                summary = self._generate_summary(img_bytes, idx)
                if summary:
                    summaries.append(summary)

                # Store image
                image_meta = self._store_image(img_bytes, pic, dl_doc, context, idx)
                if image_meta:
                    picture_images.append(image_meta)

            except Exception as e:
                logger.warning(f"Failed to process picture {idx}: {e}")
                continue

        logger.info(f"Generated {len(summaries)} summaries and {len(picture_images)} images")
        return summaries, picture_images

    def filter_pictures(
        self,
        pictures: list[PictureItem],
        dl_doc: Any,
    ) -> list[PictureItem]:
        """
        Filter out tiny/irrelevant pictures.

        Extracted from PDFParser._filter_pictures() (lines 480-505).

        Filtering criteria:
        - Remove pictures classified as decorative (logo/icon/background)
        - Remove pictures smaller than MIN_IMAGE_WIDTH x MIN_IMAGE_HEIGHT

        Args:
            pictures: List of all PictureItem objects
            dl_doc: The Docling document

        Returns:
            Filtered list of pictures worth processing
        """
        filtered: list[PictureItem] = []

        for pic in pictures:
            try:
                # Check classification label
                if self._is_excluded_by_class(pic):
                    continue

                # Check image dimensions
                if not self._meets_size_requirements(pic, dl_doc):
                    continue

                filtered.append(pic)

            except Exception:
                # Keep malformed entries (best-effort)
                filtered.append(pic)

        return filtered

    def _is_excluded_by_class(self, pic: PictureItem) -> bool:
        """
        Check if picture should be excluded based on classification.

        Extracted from PDFParser._is_excluded_by_class() (lines 460-478).

        Args:
            pic: The PictureItem to check

        Returns:
            True if the picture should be excluded
        """
        try:
            data = getattr(pic, "classification", None)
            label = getattr(data, "label", None)
            if label is None:
                return False

            # Handle both Enum and str
            label_str = str(label).lower()
            return any(excluded in label_str for excluded in EXCLUDED_CLASSIFICATIONS)

        except Exception:
            return False

    def _meets_size_requirements(self, pic: PictureItem, dl_doc: Any) -> bool:
        """
        Check if picture meets minimum size requirements.

        Args:
            pic: The PictureItem to check
            dl_doc: The Docling document

        Returns:
            True if the picture is large enough to keep
        """
        try:
            img = pic.get_image(dl_doc)
            if not img:
                return True  # Keep if we can't check (best-effort)

            w, h = img.size
            if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
                return False
            return (w * h) >= MIN_IMAGE_AREA

        except Exception:
            # If we cannot materialize the image, keep it (best-effort)
            return True

    def _extract_image_bytes(self, pic: PictureItem, dl_doc: Any) -> bytes | None:
        """
        Extract image as PNG bytes.

        Args:
            pic: The PictureItem
            dl_doc: The Docling document

        Returns:
            PNG image bytes or None if extraction failed
        """
        try:
            pil_img = pic.get_image(dl_doc)
            if not pil_img:
                return None

            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            return buf.getvalue()

        except Exception as e:
            logger.debug(f"Failed to extract image bytes: {e}")
            return None

    def _generate_summary(self, img_bytes: bytes, idx: int) -> str | None:
        """
        Generate LLM summary for an image.

        Args:
            img_bytes: The image as PNG bytes
            idx: Image index (for logging)

        Returns:
            Summary string or None if generation failed/was generic
        """
        try:
            ag_img = AgnoImage(content=img_bytes)
            # Agno V2: use positional argument instead of message=
            resp = image_summary_agent.run(
                "Analyze and summarize this image for RAG purposes",
                images=[ag_img],
            )
            content = getattr(resp, "content", None) or str(resp)

            # Filter out generic responses
            if not content:
                return None

            content_lower = content.lower()
            if any(marker in content_lower for marker in GENERIC_RESPONSE_MARKERS):
                logger.debug(f"Image {idx} got generic LLM response, skipping")
                return None

            return content

        except Exception as e:
            logger.warning(f"Failed to generate summary for image {idx}: {e}")
            return None

    def _store_image(
        self,
        img_bytes: bytes,
        pic: PictureItem,
        _dl_doc: Any,  # Reserved for future use
        context: ProcessingContext,
        idx: int,
    ) -> dict[str, Any] | None:
        """
        Store image to blob storage.

        Args:
            img_bytes: The image as PNG bytes
            pic: The PictureItem (for page info)
            _dl_doc: The Docling document (reserved for future use)
            context: Processing context
            idx: Image index

        Returns:
            Dict with {path, mimetype} or None if storage failed
        """
        if not self._storage:
            return None

        try:
            # Get page number for path planning
            pic_pages = [getattr(prov, "page_no", None) for prov in getattr(pic, "prov", [])]
            first_page = min([p for p in pic_pages if p is not None], default=None)

            # Plan blob path
            blob_path = plan_image_path_for_source(
                user_id=context.user_id,
                source_id=context.source_id,
                source_blob_path=context.source_blob_path,
                page_no=first_page,
                idx=f"pic_{idx}",
            )

            # Upload to blob storage using persistent thread-local event loop
            run_async(self._storage.upload_bytes(blob_path, img_bytes, content_type="image/png"))

            return {
                "path": blob_path,
                "mimetype": "image/png",
            }

        except Exception as e:
            logger.warning(f"Failed to store image {idx}: {e}")
            return None
