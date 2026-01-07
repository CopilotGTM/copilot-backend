"""Parser registry - singleton for registering and retrieving document parsers."""

from __future__ import annotations

from src.infra.utils.logger import get_logger

from .protocol import IDocumentParser, UnsupportedMimeTypeError

logger = get_logger("parser_registry")


class ParserRegistry:
    """
    Singleton registry for document parsers.

    Parsers register themselves using the `@ParserRegistry.register` decorator.
    The ExtractionService retrieves the appropriate parser using `get_parser()`.

    Usage:
        # Register a parser (typically done at module level)
        @ParserRegistry.register
        class PDFParser(IDocumentParser):
            ...

        # Get a parser for a MIME type
        parser = ParserRegistry.get_parser("application/pdf")
        result = parser.parse(file_path, context)

    Thread Safety:
        The registry is designed to be populated at import time (module load)
        and read during request handling. No locks are needed because:
        1. Registration happens before workers start accepting tasks
        2. After startup, the registry is read-only
    """

    _parsers: dict[str, type[IDocumentParser]] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, parser_class: type[IDocumentParser]) -> type[IDocumentParser]:
        """
        Decorator to register a parser for its supported MIME types.

        Args:
            parser_class: The parser class to register

        Returns:
            The same parser class (allows use as decorator)

        Example:
            @ParserRegistry.register
            class PDFParser(IDocumentParser):
                @classmethod
                def supported_mimetypes(cls) -> list[str]:
                    return ["application/pdf"]
        """
        try:
            mimetypes = parser_class.supported_mimetypes()
            for mimetype in mimetypes:
                normalized = mimetype.lower().strip()
                if normalized in cls._parsers:
                    logger.warning(
                        f"Overwriting parser for {normalized}: "
                        f"{cls._parsers[normalized].__name__} -> {parser_class.__name__}"
                    )
                cls._parsers[normalized] = parser_class
                logger.info(f"Registered parser {parser_class.__name__} for {normalized}")
        except Exception as e:
            logger.error(f"Failed to register parser {parser_class.__name__}: {e}")
            raise

        return parser_class

    @classmethod
    def get_parser(cls, mimetype: str) -> IDocumentParser:
        """
        Get a parser instance for the given MIME type.

        Args:
            mimetype: The MIME type to find a parser for (case-insensitive)

        Returns:
            An instance of the appropriate parser

        Raises:
            UnsupportedMimeTypeError: If no parser is registered for the MIME type
        """
        cls._ensure_initialized()

        normalized = mimetype.lower().strip()
        parser_class = cls._parsers.get(normalized)

        if parser_class is None:
            logger.warning(f"No parser found for MIME type: {normalized}")
            raise UnsupportedMimeTypeError(normalized)

        logger.debug(f"Using parser {parser_class.__name__} for {normalized}")
        return parser_class()

    @classmethod
    def get_supported_mimetypes(cls) -> list[str]:
        """
        Get list of all registered MIME types.

        Returns:
            List of supported MIME type strings
        """
        cls._ensure_initialized()
        return list(cls._parsers.keys())

    @classmethod
    def is_supported(cls, mimetype: str) -> bool:
        """
        Check if a MIME type is supported.

        Args:
            mimetype: The MIME type to check

        Returns:
            True if a parser is registered for this MIME type
        """
        cls._ensure_initialized()
        return mimetype.lower().strip() in cls._parsers

    @classmethod
    def _ensure_initialized(cls) -> None:
        """
        Ensure parsers are imported and registered.

        This is called lazily on first access to handle circular imports.
        Parsers register themselves when their modules are imported.

        Note: Heavy parsers (docling_parser) are loaded lazily here to avoid
        loading ML libraries (docling, transformers) until a document needs parsing.
        """
        if cls._initialized:
            return

        # Import light parser modules to trigger registration
        # Each parser module uses @ParserRegistry.register decorator
        try:
            from . import (
                json_parser,  # noqa: F401 - JSON parser
                text_parser,  # noqa: F401 - Text file parser
                xml_parser,  # noqa: F401 - XML parser
            )
        except ImportError as e:
            logger.warning(f"Failed to import light parsers: {e}")

        # Import heavy parsers (docling) - this loads ML libraries
        # Only imported when registry is accessed, not at worker startup
        try:
            from . import docling_parser  # noqa: F401 - PDF/DOCX parser (heavy)

            logger.info("DoclingParser loaded for PDF/DOCX support")
        except ImportError as e:
            logger.warning(f"Failed to import docling_parser: {e}")

        cls._initialized = True
        logger.info(f"Parser registry initialized with {len(cls._parsers)} parsers")

    @classmethod
    def reset(cls) -> None:
        """
        Reset the registry (for testing only).

        Clears all registered parsers and resets initialization state.
        """
        cls._parsers.clear()
        cls._initialized = False
        logger.debug("Parser registry reset")
