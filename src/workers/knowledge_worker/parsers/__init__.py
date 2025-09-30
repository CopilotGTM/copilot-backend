# Parser modules for document extraction
from .parser_factory import ParserFactory, parser_factory
from .base_parser import BaseParser
from .pdf_parser import PDFParser as DocumentParser  # Backward compatibility
from .pdf_parser import PDFParser

__all__ = [
    "ParserFactory",
    "parser_factory",
    "BaseParser",
    "PDFParser",
    "DocumentParser",  # legacy name
]
