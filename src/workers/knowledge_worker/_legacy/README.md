# Legacy Code (Archived)

This folder contains legacy code that was replaced during the knowledge worker refactoring (December 2025).

## Files

| File | Original Location | Replaced By |
|------|-------------------|-------------|
| `pdf_parser_legacy.py` | `parsers/pdf_parser.py` | `parsers/pdf_parser_v2.py` |
| `base_parser_legacy.py` | `parsers/base_parser.py` | `parsers/protocol.py` (IDocumentParser) |
| `parser_factory_legacy.py` | `parsers/parser_factory.py` | `parsers/registry.py` + `services/extraction_service.py` |
| `document_extraction_legacy.py` | `tasks/document_extraction.py` | `tasks/document_extraction.py` (v2, clean) |

## Why Archived?

The legacy code was a monolithic implementation where:
- `PDFParser` handled parsing, embedding, and storage all in one class (604 lines)
- `BaseParser` was an abstract class with mixed responsibilities
- `parser_factory` was a singleton factory with hardcoded parser list
- `document_extraction` task had both legacy and new code paths with feature flag

## New Architecture

The new architecture uses:
1. **IDocumentParser protocol** - Clean interface for parsers
2. **ParserRegistry** - Decorator-based registration
3. **ChunkProcessor** - Handles embeddings and multimodal processing
4. **ExtractionService** - Orchestrates the pipeline
5. **Clean task** - No feature flags, only uses new architecture

## Safe to Delete?

Yes, after confirming the new architecture works in production. Keep for rollback if needed.
