"""Image Parser - Direct image processing without OCR/text extraction.

This parser handles standalone image files (PNG, JPEG, etc.) by directly
using vision AI to generate rich, searchable descriptions.

Unlike DoclingParser which attempts OCR/text extraction (and fails for
images without text), this parser treats images as visual content and
uses an LLM with vision capabilities to understand and describe them.

Flow:
    Image File → Read bytes → Vision AI Summary → Searchable chunk

This is much faster and more reliable than:
    Image → Docling OCR → Maybe text → Maybe chunks → Maybe works

Metadata Handling:
    For consistency with document parsing, standalone images include:
    - raw_metadata["standalone_image"] with {path, mimetype} for the source image
    - This is merged into images_list by ChunkProcessor
    - filters["has_images"] = True
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from agno.media import Image as AgnoImage

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.agents.kb_image_summary_agent import kb_image_summary_agent
from src.workers.knowledge_worker.models import ChunkData, ParsedContent, ProcessingContext
from src.workers.knowledge_worker.parsers.protocol import IDocumentParser
from src.workers.knowledge_worker.parsers.registry import ParserRegistry

if TYPE_CHECKING:
    pass

logger = get_logger("image_parser")

# Markers that indicate the LLM couldn't process the image
GENERIC_RESPONSE_MARKERS = [
    "provide the image",
    "cannot see",
    "unable to process",
    "no image",
    "cannot analyze",
]


@ParserRegistry.register
class ImageParser(IDocumentParser):
    """
    Parser for standalone image files using vision AI.

    This parser bypasses OCR/text extraction entirely and uses a vision-capable
    LLM to generate rich descriptions of images. This approach:

    1. Always works - doesn't depend on OCR finding text
    2. Is faster - no Docling/OCR overhead
    3. Produces better results - vision AI understands visual content

    Supported formats: PNG, JPEG, GIF, BMP, TIFF, WebP

    Usage:
        parser = ImageParser()
        result = parser.parse(file_path, context)
        # result.chunks[0].text contains the AI-generated description
    """

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Return MIME types this parser handles."""
        return [
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/gif",
            "image/bmp",
            "image/tiff",
            "image/webp",
        ]

    def parse(
        self,
        file_path: str | Path,
        context: ProcessingContext,
    ) -> ParsedContent:
        """
        Parse an image file using vision AI.

        Args:
            file_path: Path to the image file
            context: Processing context with org/source/user info

        Returns:
            ParsedContent with a single chunk containing the image description
        """
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"Image file not found: {file_path}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=True
            )

        logger.info(f"Processing image: {file_path.name} (source: {context.source_id})")

        try:
            # 1. Read image bytes
            img_bytes = file_path.read_bytes()
            logger.debug(f"Read {len(img_bytes)} bytes from {file_path.name}")

            # 2. Generate summary using vision AI
            summary_text = self._generate_image_summary(img_bytes, context)

            if not summary_text:
                logger.warning(f"Failed to generate summary for {file_path.name}")
                return ParsedContent(
                    chunks=[],
                    metadata={"error": "Failed to generate image summary"},
                    supports_tables=False,
                    supports_images=True,
                )

            # 3. Build image metadata (similar to how documents store image references)
            # The source_blob_path already contains the uploaded image path
            image_mimetype = context.mimetype or self._guess_mimetype(file_path)
            standalone_image_info = {
                "path": context.source_blob_path,
                "mimetype": image_mimetype,
            }

            # 4. Create chunk with the summary as searchable text
            chunk = ChunkData(
                index=0,
                text=summary_text,
                page_start=1,
                page_end=1,
                tables=[],
                pictures=[],  # Empty - we use standalone_image in raw_metadata instead
                raw_metadata={
                    "parser": "ImageParser",
                    "source_type": "standalone_image",
                    "original_filename": context.original_filename,
                    "image_size_bytes": len(img_bytes),
                    # This will be merged into images_list by ChunkProcessor
                    "standalone_image": standalone_image_info,
                },
            )

            logger.info(f"Generated summary for {file_path.name}: {len(summary_text)} chars")

            return ParsedContent(
                chunks=[chunk],
                metadata={
                    "parser": "ImageParser",
                    "original_filename": context.original_filename,
                },
                supports_tables=False,
                supports_images=True,
            )

        except Exception as e:
            logger.error(f"Failed to process image {file_path}: {e}")
            return ParsedContent(
                chunks=[],
                metadata={"error": str(e)},
                supports_tables=False,
                supports_images=True,
            )

    def _generate_image_summary(
        self,
        img_bytes: bytes,
        context: ProcessingContext,
    ) -> str | None:
        """
        Generate a searchable summary of the image using vision AI.

        Args:
            img_bytes: Raw image bytes
            context: Processing context

        Returns:
            Summary text or None if generation failed
        """
        try:
            # Create Agno image from bytes
            ag_img = AgnoImage(content=img_bytes)

            # Call the KB image summary agent
            response = kb_image_summary_agent.run(
                "Analyze this image and provide a comprehensive description for knowledge base search.",
                images=[ag_img],
            )

            content = getattr(response, "content", None) or str(response)

            if not content:
                logger.warning("Empty response from image summary agent")
                return None

            # Check for generic failure responses
            content_lower = content.lower()
            if any(marker in content_lower for marker in GENERIC_RESPONSE_MARKERS):
                logger.warning(f"Generic response detected for image: {content[:100]}")
                return None

            # Try to parse as JSON and extract key fields for a rich text summary
            searchable_text = self._json_to_searchable_text(content, context)

            return searchable_text

        except Exception as e:
            logger.error(f"Failed to generate image summary: {e}")
            return None

    def _json_to_searchable_text(self, json_content: str, context: ProcessingContext) -> str:
        """
        Convert JSON summary to searchable text format.

        The agent returns JSON, but we want rich text for embedding/search.
        This extracts key fields and formats them for optimal retrieval.

        Args:
            json_content: JSON string from the agent
            context: Processing context

        Returns:
            Formatted searchable text
        """
        try:
            # Try to parse JSON
            data = json.loads(json_content)

            sections = []

            # Title/Type
            if data.get("title"):
                sections.append(f"# {data['title']}")
            if data.get("type"):
                sections.append(f"Type: {data['type']}")

            # Primary subject
            if data.get("primary_subject"):
                sections.append(f"Subject: {data['primary_subject']}")

            # Purpose
            if data.get("purpose"):
                sections.append(f"Purpose: {data['purpose']}")

            # Context
            if data.get("context"):
                sections.append(f"Context: {data['context']}")

            # Summary (most important for search)
            if data.get("summary"):
                sections.append(f"\n{data['summary']}")

            # Text content (crucial for searchability)
            if data.get("text_content"):
                text_content = data["text_content"]
                if isinstance(text_content, list):
                    text_content = "\n".join(str(t) for t in text_content)
                sections.append(f"\nVisible Text:\n{text_content}")

            # Entities
            if data.get("entities") and isinstance(data["entities"], list):
                sections.append(f"\nEntities: {', '.join(str(e) for e in data['entities'])}")

            # Key insights
            if data.get("key_insights") and isinstance(data["key_insights"], list):
                insights = "\n".join(f"- {insight}" for insight in data["key_insights"])
                sections.append(f"\nKey Insights:\n{insights}")

            # Data points
            if data.get("data_points") and isinstance(data["data_points"], list):
                data_points = ", ".join(str(d) for d in data["data_points"])
                sections.append(f"\nData Points: {data_points}")

            # Visual elements
            if data.get("visual_elements") and isinstance(data["visual_elements"], list):
                elements = ", ".join(str(e) for e in data["visual_elements"])
                sections.append(f"\nVisual Elements: {elements}")

            # Relationships
            if data.get("relationships") and isinstance(data["relationships"], list):
                relationships = "; ".join(str(r) for r in data["relationships"])
                sections.append(f"\nRelationships: {relationships}")

            # Tags (for keyword search)
            if data.get("retrieval_tags") and isinstance(data["retrieval_tags"], list):
                tags = " ".join(f"#{tag}" for tag in data["retrieval_tags"])
                sections.append(f"\nTags: {tags}")

            # Add filename for context
            if context.original_filename:
                sections.append(f"\nSource: {context.original_filename}")

            return "\n".join(sections)

        except json.JSONDecodeError:
            # If JSON parsing fails, use the raw content
            logger.debug("Could not parse agent response as JSON, using raw content")
            return json_content

    def _guess_mimetype(self, file_path: Path) -> str:
        """
        Guess MIME type from file extension.

        Args:
            file_path: Path to the file

        Returns:
            MIME type string, defaults to "image/png" if unknown
        """
        guessed_type, _ = mimetypes.guess_type(str(file_path))
        return guessed_type or "image/png"
