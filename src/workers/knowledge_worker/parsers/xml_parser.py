"""XML Parser - Structure-aware parser for XML files.

This module provides a parser for XML files that:
1. Implements IDocumentParser protocol
2. Uses structure-aware chunking (splits at root's direct child elements)
3. Preserves XML structure for LLM understanding
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # nosec B405 - only used for tostring/indent, not parsing
from pathlib import Path

import defusedxml.ElementTree as DefusedET

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.models import ChunkData, ParsedContent, ProcessingContext
from src.workers.knowledge_worker.parsers.protocol import IDocumentParser
from src.workers.knowledge_worker.parsers.registry import ParserRegistry

logger = get_logger("xml_parser")

# Maximum size for a single chunk (in characters)
MAX_CHUNK_SIZE = 4000


@ParserRegistry.register
class XmlParser(IDocumentParser):
    """
    Structure-aware parser for XML files.

    Chunking Strategy:
    1. Small XML (< MAX_CHUNK_SIZE): Return as single chunk.
    2. Large XML: Split by root's direct child elements.
    3. Group small elements together until they approach max_chunk_size.

    This ensures we never break in the middle of an XML element.
    """

    def __init__(self, max_chunk_size: int = MAX_CHUNK_SIZE):
        self.max_chunk_size = max_chunk_size

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Return MIME types this parser supports."""
        return ["application/xml", "text/xml"]

    def parse(
        self,
        file_path: str | Path,
        context: ProcessingContext,
    ) -> ParsedContent:
        """Parse an XML file and return structured content."""
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

        logger.info(f"Parsing XML file: {file_path.name} (source: {context.source_id})")

        try:
            content = file_path.read_text(encoding="utf-8")

            try:
                root = DefusedET.fromstring(content)
            except ET.ParseError as e:
                logger.warning(f"Invalid XML in {file_path}: {e}, treating as plain text")
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
                    metadata={"error": "invalid_xml", "original_size": len(content)},
                    supports_tables=False,
                    supports_images=False,
                )

            # Structure-aware chunking
            chunks = self._create_structure_aware_chunks(root)

            logger.info(f"Parsed {len(chunks)} chunks from {file_path.name}")

            return ParsedContent(
                chunks=chunks,
                metadata={
                    "original_size": len(content),
                    "chunk_count": len(chunks),
                    "root_tag": root.tag,
                    "child_count": len(list(root)),
                },
                supports_tables=False,
                supports_images=False,
            )

        except Exception as e:
            logger.error(f"Failed to parse XML file {file_path}: {e}")
            return ParsedContent(
                chunks=[], metadata={}, supports_tables=False, supports_images=False
            )

    def _element_to_string(self, element: ET.Element) -> str:
        """Convert an XML element to a formatted string."""
        return ET.tostring(element, encoding="unicode")

    def _indent_xml(self, xml_string: str) -> str:
        """Add basic indentation to XML string for readability."""
        try:
            # Parse and re-serialize with indentation (Python 3.9+)
            element = DefusedET.fromstring(xml_string)
            ET.indent(element)
            return ET.tostring(element, encoding="unicode")
        except Exception:
            return xml_string

    def _create_structure_aware_chunks(self, root: ET.Element) -> list[ChunkData]:
        """
        Create chunks based on XML structure.

        Strategy:
        - If entire XML fits in max_chunk_size, return as single chunk.
        - Otherwise, split by root's direct children.
        - Group small children together until they approach max_chunk_size.
        """
        # Try full document first
        full_xml = self._element_to_string(root)
        full_xml = self._indent_xml(full_xml)

        if len(full_xml) <= self.max_chunk_size:
            return [
                ChunkData(
                    index=0,
                    text=full_xml,
                    page_start=None,
                    page_end=None,
                    tables=[],
                    pictures=[],
                )
            ]

        # Split by root's direct children
        children = list(root)

        if not children:
            # No children, return the whole thing as one chunk
            return [
                ChunkData(
                    index=0,
                    text=full_xml,
                    page_start=None,
                    page_end=None,
                    tables=[],
                    pictures=[],
                )
            ]

        chunks: list[ChunkData] = []
        current_elements: list[ET.Element] = []
        current_size = 0

        for child in children:
            child_xml = self._element_to_string(child)
            child_size = len(child_xml)

            # If adding this child would exceed limit, flush current batch
            if current_elements and (current_size + child_size > self.max_chunk_size):
                chunks.append(
                    self._create_chunk_from_elements(len(chunks), current_elements, root.tag)
                )
                current_elements = []
                current_size = 0

            current_elements.append(child)
            current_size += child_size

        # Flush remaining elements
        if current_elements:
            chunks.append(self._create_chunk_from_elements(len(chunks), current_elements, root.tag))

        return chunks

    def _create_chunk_from_elements(
        self, index: int, elements: list[ET.Element], root_tag: str
    ) -> ChunkData:
        """Create a ChunkData from a list of XML elements, wrapped in root tag."""
        # Create a wrapper element
        wrapper = ET.Element(root_tag)
        for elem in elements:
            wrapper.append(elem)

        xml_text = self._element_to_string(wrapper)
        xml_text = self._indent_xml(xml_text)

        return ChunkData(
            index=index,
            text=xml_text,
            page_start=None,
            page_end=None,
            tables=[],
            pictures=[],
        )
