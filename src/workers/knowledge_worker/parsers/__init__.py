# Parser modules for document extraction
from .base_parser import BaseParser
from .parser_factory import ParserFactory, parser_factory
from .pdf_parser import PDFParser
from .pdf_parser import PDFParser as DocumentParser  # Backward compatibility

__all__ = [
    "ParserFactory",
    "parser_factory",
    "BaseParser",
    "PDFParser",
    "DocumentParser",  # legacy name
]
