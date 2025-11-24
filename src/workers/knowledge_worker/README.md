# Knowledge Worker

This worker handles knowledge base-related background tasks, specifically document processing and knowledge source management.

## Tasks

### knowledge.document_extraction

- **Queue**: `knowledge_extract`
- **Purpose**: Heavy document extraction task for PDF/DOC files
- **Args**: `organisation_id: str, source_id: str`
- **Retries**: 3 max retries
- **Features**:
  - Processes uploaded documents for knowledge base
  - Updates source status to COMPLETED or FAILED
  - Publishes status updates via messaging

### knowledge.post_upload_process

- **Queue**: `knowledge_upload`
- **Purpose**: Fast post-upload processing (checksum calculation, thumbnail generation)
- **Args**: `organisation_id: str, source_id: str`
- **Features**:
  - Calculates file checksums
  - Generates thumbnails for images and PDFs
  - Updates source metadata

### Test Tasks

- **knowledge.ping**: Simple connectivity test
- **knowledge.test_task**: Test task with message parameter

## Queues

- **knowledge_extract**: For heavy document extraction tasks
- **knowledge_upload**: For fast post-upload processing tasks

## Usage

### Start the worker

```powershell
celery -A src.workers.knowledge_worker.celery_app worker -l info -Q knowledge_extract,knowledge_upload --pool=solo
```

### Test connectivity

```powershell
python -c "from src.workers.knowledge_worker.celery_app import app; result = app.send_task('knowledge.ping'); print(result.get())"
```

## Configuration

- **Exchange**: `knowledge` (direct)
- **Default Queue**: `knowledge_upload`
- **Default Routing Key**: `knowledge_upload`
- **Environment**: Uses same .env file as other workers

### Queue Configuration

The worker uses a dedicated `knowledge` exchange with proper routing keys to avoid task routing issues:

- **knowledge_extract**: For document extraction tasks
- **knowledge_upload**: For post-upload processing and test tasks

### Task Routes

```python
{
    "knowledge.document_extraction": {"queue": "knowledge_extract", "routing_key": "knowledge_extract"},
    "knowledge.post_upload_process": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
    "knowledge.test_task": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
    "knowledge.ping": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
}
```

## Task Separation

This worker focuses on knowledge base operations:

- **Document extraction**: Heavy processing of PDF/DOC files
- **Post-upload processing**: Fast checksum and thumbnail generation
- **Knowledge source management**: Status updates and metadata handling

For chat-related attachments, use the chat worker instead.

## Dependencies

- **PIL**: For image processing and thumbnail generation
- **fitz (PyMuPDF)**: For PDF processing
- **Azure Blob Storage**: For file storage operations
- **SQLAlchemy**: For database operations

## Error Handling

- Tasks include retry mechanisms with exponential backoff
- Failed tasks update source status appropriately
- Comprehensive logging for debugging
- Status updates published via messaging system
