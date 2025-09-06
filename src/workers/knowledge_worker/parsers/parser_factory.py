from __future__ import annotations

from typing import Dict, List, Type, Optional, Any
from pathlib import Path

from .base_parser import BaseParser
from .pdf_parser import PDFParser
from src.infra.utils.logger import get_logger

logger = get_logger("parser_factory")


class ParserFactory:
    """Factory class for creating document parsers based on MIME type."""

    def __init__(self):
        """Initialize the parser factory with available parsers."""
        self._parsers: Dict[str, Type[BaseParser]] = {}
        self._register_default_parsers()

    def _register_default_parsers(self):
        """Register all available parsers."""
        parsers = [PDFParser]

        for parser_class in parsers:
            supported_types: List[str] = []
            # Prefer lightweight classmethod if provided
            try:
                cls_method = getattr(parser_class, "supported_mimetypes", None)
                if callable(cls_method):
                    types = cls_method()
                    if isinstance(types, list):
                        supported_types = [str(t) for t in types]
            except Exception as e:
                logger.debug(
                    f"Classmethod supported_mimetypes failed for {parser_class.__name__}: {e}"
                )

            # Fallback to instantiation for legacy parsers
            if not supported_types:
                try:
                    temp_parser = parser_class(
                        organisation_id="temp",
                        user_id="temp",
                        storage_backend=None,
                    )
                    supported_types = temp_parser.get_supported_mimetypes()
                except Exception as e:
                    logger.warning(
                        f"Skipping parser {parser_class.__name__}: cannot determine supported mimetypes ({e})"
                    )
                    continue

            for mime_type in supported_types:
                self._parsers[mime_type.lower()] = parser_class
                logger.info(
                    f"Registered parser {parser_class.__name__} for MIME type: {mime_type}"
                )

    def get_supported_mimetypes(self) -> List[str]:
        """Get list of all supported MIME types."""
        return list(self._parsers.keys())

    def get_parser(
        self, mimetype: str, organisation_id: str, user_id: str, storage_backend: Any
    ) -> Optional[BaseParser]:
        """
        Get a parser instance for the given MIME type.

        Args:
            mimetype: The MIME type of the document
            organisation_id: The organisation ID
            user_id: The user ID who uploaded the document
            storage_backend: Storage backend instance

        Returns:
            Parser instance if supported, None otherwise
        """
        if not mimetype:
            logger.warning("No MIME type provided")
            return None

        mimetype_lower = mimetype.lower().strip()
        parser_class = self._parsers.get(mimetype_lower)

        if not parser_class:
            logger.warning(f"No parser found for MIME type: {mimetype}")
            return None

        try:
            parser_instance = parser_class(
                organisation_id=organisation_id,
                user_id=user_id,
                storage_backend=storage_backend,
            )
            logger.info(
                f"Created parser {parser_class.__name__} for MIME type: {mimetype}"
            )
            return parser_instance

        except Exception as e:
            logger.error(f"Error creating parser for MIME type {mimetype}: {str(e)}")
            return None

    def parse_document(
        self,
        file_path: str | Path,
        mimetype: str,
        organisation_id: str,
        user_id: str,
        storage_backend: Any,
        document_url: Optional[str] = None,
        source_id: Optional[str] = None,
        source_blob_path: Optional[str] = None,
        original_filename: Optional[str] = None,
        source_size_bytes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Parse a document using the appropriate parser.

        Args:
            file_path: Path to the document file
            mimetype: The MIME type of the document
            organisation_id: The organisation ID
            user_id: The user ID who uploaded the document
            storage_backend: Storage backend instance
            document_url: Optional URL of the document

        Returns:
            List of processed document chunks/pages
        """
        parser = self.get_parser(mimetype, organisation_id, user_id, storage_backend)

        if not parser:
            logger.error(
                f"Cannot parse document: no parser available for MIME type {mimetype}"
            )
            return []

        try:
            return parser.parse(
                file_path=file_path,
                document_url=document_url,
                source_id=source_id,
                source_blob_path=source_blob_path,
                original_filename=original_filename,
                source_size_bytes=source_size_bytes,
            )
        except Exception as e:
            logger.error(f"Error parsing document {file_path}: {str(e)}")
            return []

    def register_parser(self, parser_class: Type[BaseParser], mimetypes: List[str]):
        """
        Register a custom parser for specific MIME types.

        Args:
            parser_class: The parser class to register
            mimetypes: List of MIME types this parser supports
        """
        for mimetype in mimetypes:
            self._parsers[mimetype.lower()] = parser_class
            logger.info(
                f"Registered custom parser {parser_class.__name__} for MIME type: {mimetype}"
            )


# Global parser factory instance
parser_factory = ParserFactory()
