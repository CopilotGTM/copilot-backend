# Parser modules for document extraction
#
# Note: Heavy parsers (DoclingParser) are NOT imported at module level
# to avoid loading heavy dependencies (docling, transformers) until needed.
# Use ParserRegistry.get_parser(mimetype) to get the appropriate parser.

# Light parsers - imported eagerly (no heavy dependencies)
from .json_parser import JsonParser
from .protocol import IDocumentParser, UnsupportedMimeTypeError
from .registry import ParserRegistry
from .text_parser import TextParser
from .xml_parser import XmlParser

__all__ = [
    # Core protocol and registry
    "IDocumentParser",
    "UnsupportedMimeTypeError",
    "ParserRegistry",
    # Light parsers (no heavy dependencies)
    "JsonParser",
    "TextParser",
    "XmlParser",
    # Heavy parsers - use get_docling_parser() for lazy loading
    "get_docling_parser",
]


def get_docling_parser() -> type:
    """Lazy import DoclingParser to avoid loading heavy dependencies at startup.

    Use this function instead of directly importing DoclingParser to defer
    loading of docling, transformers, and other ML libraries until actually needed.

    Returns:
        The DoclingParser class (not an instance)

    Example:
        DoclingParser = get_docling_parser()
        parser = DoclingParser()
        result = parser.parse(file_path, context)
    """
    from .docling_parser import DoclingParser

    return DoclingParser
