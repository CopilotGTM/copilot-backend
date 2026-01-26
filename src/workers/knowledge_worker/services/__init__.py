"""Services package for knowledge worker.

This package contains extracted services following the single responsibility principle:
- TableProcessor: Table extraction, LLM summarization, storage
- ImageProcessor: Image extraction, filtering, LLM summarization, storage
- ChunkProcessor: Orchestrates chunk processing (tables + images + embeddings)
- ExtractionService: High-level orchestration of the full extraction pipeline
- TaskHelperService: Status management and validation for Celery tasks

All services use AzureOrganisationBlobStorage directly with an event loop
for async operations (standard pattern in this codebase).
"""

from __future__ import annotations

from src.workers.knowledge_worker.services.chunk_processor import ChunkProcessor
from src.workers.knowledge_worker.services.extraction_service import ExtractionService
from src.workers.knowledge_worker.services.image_processor import ImageProcessor
from src.workers.knowledge_worker.services.post_upload_service import PostUploadService
from src.workers.knowledge_worker.services.table_processor import TableProcessor
from src.workers.knowledge_worker.services.task_helpers import TaskHelperService

__all__ = [
    "ChunkProcessor",
    "ExtractionService",
    "ImageProcessor",
    "PostUploadService",
    "TableProcessor",
    "TaskHelperService",
]
