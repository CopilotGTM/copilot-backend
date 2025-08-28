# Document Parser System

## Overview

The Document Parser System is a production-grade, extensible framework for parsing various document formats in the Knowledge Worker. It supports multiple document types with intelligent content extraction, embedding generation, and metadata processing.

## Supported Formats

| Format | MIME Type | Status | Library Used |
|--------|-----------|--------|--------------|
| PDF | `application/pdf` | ✅ Production Ready | PyMuPDF, pypdf |
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | ✅ Production Ready | python-docx |
| DOC | `application/msword` | ⚠️ Not Supported | - |
| PPTX | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | ✅ Production Ready | python-pptx |
| PPT | `application/vnd.ms-powerpoint` | ⚠️ Legacy Format | - |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Parser Factory                          │
│  Routes documents to appropriate parsers based on MIME type │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   ┌────▼───┐   ┌────▼───┐   ┌────▼───┐
   │  PDF   │   │  DOC   │   │  PPT   │
   │ Parser │   │ Parser │   │ Parser │
   └────┬───┘   └────┬───┘   └────┬───┘
        │             │             │
        └─────────────┼─────────────┘
                      │
        ┌─────────────▼─────────────┐
        │       Utility Services     │
        │  • Embedding Service      │
        │  • Content Processor      │
        │  • Document Store         │
        └───────────────────────────┘
```

## Key Components

### 1. Parser Factory (`parser_factory.py`)
- **Purpose**: Routes documents to appropriate parsers based on MIME type
- **Features**: 
  - Automatic parser registration
  - Error handling for unsupported formats
  - Easy extensibility for new parsers

### 2. Base Parser (`base_parser.py`)
- **Purpose**: Abstract base class defining parser interface
- **Features**: Consistent API across all parsers

### 3. Utility Services

#### Embedding Service (`utils/embedding_service.py`)
- **Purpose**: Generate embeddings for document content
- **Features**:
  - Azure OpenAI integration
  - Text length validation and truncation
  - Batch processing support
  - Error handling with fallback

#### Content Processor (`utils/content_processor.py`)
- **Purpose**: Process and clean document content
- **Features**:
  - Text cleaning and normalization
  - Content chunking with overlap
  - Metadata extraction from filenames
  - Content validation

#### Document Store (`utils/document_store.py`)
- **Purpose**: Store processed documents in database
- **Features**:
  - Batch document storage
  - Update existing documents
  - Source-based deletion
  - Transaction management

## Usage Examples

### Basic Document Parsing

```python
from src.workers.knowledge_worker.parsers import parser_factory

# Parse a document
documents = parser_factory.parse_document(
    file_path="/path/to/document.pdf",
    mimetype="application/pdf",
    organisation_id="org-123",
    user_id="user-456",
    storage_backend=azure_storage,
    document_url="https://example.com/doc.pdf"
)

# Process results
for doc in documents:
    print(f"Document ID: {doc['id']}")
    print(f"Content: {doc['content'][:100]}...")
    print(f"Embedding dimension: {len(doc['embedding'])}")
```

### Integration with Document Extraction Task

```python
from src.workers.knowledge_worker.tasks.document_extraction import document_extraction

# The task automatically uses the parser factory
document_extraction.delay(organisation_id="org-123", source_id="source-456")
```

### Adding a Custom Parser

```python
from src.workers.knowledge_worker.parsers import BaseParser, parser_factory

class CustomParser(BaseParser):
    def get_supported_mimetypes(self):
        return ["application/custom"]
    
    def parse(self, file_path, document_url=None):
        # Implementation here
        return processed_documents

# Register the custom parser
parser_factory.register_parser(CustomParser, ["application/custom"])
```

## Configuration

Configuration is managed through environment variables and the `ParserConfig` class:

```python
from src.workers.knowledge_worker.parsers.config import ParserConfig

# Get parser-specific settings
settings = ParserConfig.get_parser_settings('pdf')
```

### Key Configuration Options

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Max Chunk Size | `MAX_CHUNK_SIZE` | 4000 | Maximum characters per chunk |
| Chunk Overlap | `CHUNK_OVERLAP` | 200 | Characters to overlap between chunks |
| Min Content Length | `MIN_CONTENT_LENGTH` | 10 | Minimum content length to process |
| Embedding Text Length | `MAX_EMBEDDING_TEXT_LENGTH` | 8000 | Maximum text length for embeddings |
| PPT Max Workers | `PPT_MAX_WORKERS` | 5 | Parallel workers for PPT processing |

## Error Handling

The parser system includes comprehensive error handling:

1. **File-level errors**: Missing files, permission issues
2. **Format-level errors**: Corrupted documents, unsupported versions
3. **Content-level errors**: Empty content, encoding issues
4. **Service-level errors**: Embedding service failures, storage issues

All errors are logged with appropriate detail levels and don't crash the entire processing pipeline.

## Performance Considerations

### PDF Parser
- Uses PyMuPDF for better performance and accuracy
- Falls back to pypdf for compatibility
- Processes pages individually to handle large documents

### PPT Parser
- Supports parallel slide processing (configurable workers)
- Handles image compression for optimal storage
- Extracts text, tables, and image metadata

### DOC Parser
- Chunks large documents for better embedding quality
- Extracts table content preserving structure
- Processes embedded images and metadata

## Testing

Run the comprehensive test suite:

```bash
# Activate virtual environment
.venv\Scripts\activate

# Run parser factory tests
python src\workers\knowledge_worker\parsers\test_parser_factory.py

# Run comprehensive tests with sample files
python src\workers\knowledge_worker\parsers\test_comprehensive.py
```

## Monitoring and Logging

All parsers use structured logging with the following levels:

- **INFO**: Processing progress, successful operations
- **WARNING**: Non-critical issues, fallback usage
- **ERROR**: Failed operations, with full error context
- **DEBUG**: Detailed processing information

Log example:
```
2025-08-25 10:30:45 INFO [pdf_parser] Starting PDF parsing for file: /path/to/document.pdf
2025-08-25 10:30:46 INFO [pdf_parser] Processing PDF with 25 pages using PyMuPDF
2025-08-25 10:30:48 INFO [pdf_parser] Successfully processed 23 pages from PDF using PyMuPDF
```

## Future Enhancements

1. **Additional Formats**: Excel (.xlsx), CSV, JSON, XML
2. **OCR Integration**: For scanned documents and images
3. **Table Detection**: Advanced table structure recognition
4. **Multi-language Support**: Language detection and processing
5. **Async Processing**: Non-blocking document processing
6. **Caching**: Parser result caching for performance

## Troubleshooting

### Common Issues

1. **"No parser found for MIME type"**
   - Verify the MIME type is correctly detected
   - Check if the format is supported

2. **"Empty content extracted"**
   - Verify the document isn't corrupted
   - Check if the document contains extractable text

3. **"Embedding service failed"**
   - Verify Azure OpenAI credentials
   - Check network connectivity

4. **"Storage operation failed"**
   - Verify storage backend configuration
   - Check permissions and connection

### Debug Mode

Enable debug logging for detailed troubleshooting:

```python
import logging
logging.getLogger("pdf_parser").setLevel(logging.DEBUG)
logging.getLogger("ppt_parser").setLevel(logging.DEBUG)
logging.getLogger("doc_parser").setLevel(logging.DEBUG)
```
