from __future__ import annotations
"""Heavy document extraction task (PDF/DOC extraction).
"""
import asyncio
import os
import tempfile
from pathlib import Path

from src.workers.knowledge_worker.celery_app import app
from src.infra.utils.logger import get_logger
from src.infra.utils.database import get_org_session
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.domain.models.organisation_tables.job_models import (
    KnowledgeProcessingJob,
    JobStatus,
)
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.workers.knowledge_worker.parsers import parser_factory
from src.workers.knowledge_worker.utils import DocumentStore

logger = get_logger("knowledge_document_extraction")


@app.task(name="knowledge.document_extraction", bind=True, acks_late=True, max_retries=3)
def document_extraction(self, organisation_id: str, source_id: str):  # pragma: no cover
    """Heavy extraction task (PDF/DOC etc.)."""
    logger.info(f"Starting document extraction for source {source_id} in org {organisation_id}")
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    temp_file_path = None
    
    try:
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)

        # Check if source exists and is not in a terminal state
        if not source:
            logger.warning("Source %s not found for extract", source_id)
            return

        # # Normalise status to string for comparisons (can be Enum or raw str)
        # current_status_val = source.status.value if hasattr(source.status, "value") else str(source.status)
        # if current_status_val in {OrganisationSourceStatus.COMPLETED.value, OrganisationSourceStatus.FAILED.value}:
        #     logger.info(f"Source {source_id} already in terminal state: {current_status_val}")
        #     return

        # Fetch job row
        job = session.query(KnowledgeProcessingJob).filter_by(source_id=source_id, job_type='document_extraction').first()
        if not job:
            logger.warning(f"No document_extraction job found for source {source_id}")
            return

        # # Update job status to processing
        # job.status = JobStatus.PROCESSING
        # session.add(job)
        # session.commit()

        # # Update source status to processing
        # source.status = OrganisationSourceStatus.PROCESSING
        # session.add(source)
        # session.commit()

        # Check if we have a supported parser for this MIME type
        mimetype = source.mimetype
        if not mimetype:
            logger.error(f"No MIME type found for source {source_id}")
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = "No MIME type specified"
            job.status = JobStatus.FAILED
            session.add(source)
            session.add(job)
            session.commit()
            return

        # Check if parser is available for this MIME type
        supported_types = parser_factory.get_supported_mimetypes()
        if mimetype.lower() not in supported_types:
            logger.warning(f"No parser available for MIME type {mimetype}")
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = f"Unsupported document type: {mimetype}"
            job.status = JobStatus.FAILED
            session.add(source)
            session.add(job)
            session.commit()
            return

        # Download the file from storage
        if not source.storage_path:
            logger.error(f"No storage path found for source {source_id}")
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = "No storage path found"
            job.status = JobStatus.FAILED
            session.add(source)
            session.add(job)
            session.commit()
            return

        # Create a temporary file to store the downloaded content
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(source.filename or "document").suffix) as temp_file:
            temp_file_path = temp_file.name
            
            # Download file from storage
            logger.info(f"Downloading file from storage: {source.storage_path}")
            file_content = asyncio.run(storage.get_blob_content(source.storage_path))
            temp_file.write(file_content)
            temp_file.flush()

        # Parse the document using the appropriate parser
        logger.info(f"Starting document parsing for source {source_id} with MIME type {mimetype}")
        
        parsed_documents = parser_factory.parse_document(
            file_path=temp_file_path,
            mimetype=mimetype,
            organisation_id=organisation_id,
            user_id=str(source.user_id),
            storage_backend=storage,
            document_url=source.filename,
            source_id=str(source.source_id),
            source_blob_path=source.storage_path,
            original_filename=source.filename,
            source_size_bytes=source.size_bytes if hasattr(source, 'size_bytes') else None,
        )

        if not parsed_documents:
            logger.error(f"No content extracted from document {source_id}")
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = "Failed to extract content from document"
            job.status = JobStatus.FAILED
            session.add(source)
            session.add(job)
            session.commit()
            return

        logger.info(f"Successfully parsed document {source_id}, extracted {len(parsed_documents)} chunks")

        # Remove any previous chunks for this source (idempotent re-run), then store
        document_store = DocumentStore(organisation_id)
        try:
            deleted = document_store.delete_documents_by_source(source_id)
            logger.info(f"Deleted {deleted} existing chunks for source {source_id}")
        except Exception as e:
            logger.warning(f"Failed to delete existing chunks for source {source_id}: {e}")
        stored_ids = document_store.store_documents(parsed_documents, source_id, str(source.user_id) if source and source.user_id else None)
        
        if not stored_ids:
            logger.error(f"Failed to store parsed documents for source {source_id}")
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = "Failed to store parsed document content"
            job.status = JobStatus.FAILED
            session.add(source)
            session.add(job)
            session.commit()
            return

        logger.info(f"Successfully stored {len(stored_ids)} document chunks for source {source_id}")

        # Update status to completed
        source.status = OrganisationSourceStatus.COMPLETED
        job.status = JobStatus.COMPLETED
        session.add(source)
        session.add(job)
        session.commit()

        # Prepare safe status value (Enum or str)
        final_status_val = source.status.value if hasattr(source.status, "value") else str(source.status)

        publish_event = KnowledgeSourceEventUpdate(
            source_id=str(source.source_id),
            status=final_status_val,
            updated_at=source.updated_at.isoformat() if source.updated_at else None,
            error_message=source.error_message,
            progress=None,
            thumbnail_medium_url=None,
            thumbnail_small_url=None,
        )

        publish_source_status(organisation_id, publish_event)
        logger.info("Published source status update", extra={"event": publish_event})

    except Exception as e:
        logger.error(f"Error during document extraction for source {source_id}: {str(e)}")
        
        # Update status to failed
        try:
            if session and source:
                source.status = OrganisationSourceStatus.FAILED
                source.error_message = f"Document extraction failed: {str(e)}"
                session.add(source)
                
                if job:
                    job.status = JobStatus.FAILED
                    session.add(job)
                
                session.commit()

                # Publish failure status
                final_status_val = source.status.value if hasattr(source.status, "value") else str(source.status)
                publish_event = KnowledgeSourceEventUpdate(
                    source_id=str(source.source_id),
                    status=final_status_val,
                    updated_at=source.updated_at.isoformat() if source.updated_at else None,
                    error_message=source.error_message,
                    progress=None,
                    thumbnail_medium_url=None,
                    thumbnail_small_url=None,
                )
                publish_source_status(organisation_id, publish_event)
        except Exception as publish_error:
            logger.error(f"Error publishing failure status: {str(publish_error)}")
        
        # Re-raise the exception for Celery retry logic
        raise
        
    finally:
        # Clean up temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
                logger.debug(f"Cleaned up temporary file: {temp_file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary file {temp_file_path}: {str(e)}")
        
        # Close database session
        try:
            session.close()
        except Exception:
            pass
            
        # Close storage connection
        try:
            asyncio.run(storage.close())
        except Exception:
            pass
