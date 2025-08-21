from __future__ import annotations

from typing import List, Tuple, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime

from sqlmodel import Session, text

from src.infra.utils.logger import get_logger
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.schemas.knowledge_upload_models import (
    KnowledgeBatchPresignRequest,
    KnowledgeFinalizeRequest,
)
from src.infra.utils.resource_naming import get_schema_name

logger = get_logger(__name__)


class KnowledgeUploadServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class KnowledgeUploadService:
    def __init__(self, org_session: Session, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)
        self.schema_name = get_schema_name(self.organisation_id)

    def _set_schema(self):
        self.org_session.execute(text(f"SET search_path TO {self.schema_name}"))

    def _plan_blob_path(self, user_id: UUID, source_id: UUID, filename: str) -> str:
        return f"knowledge_sources/{user_id}/{source_id}/{filename}"

    async def presign_batch(
        self, payload: KnowledgeBatchPresignRequest, user_id: UUID
    ) -> Tuple[List[Tuple[OrganisationSource, str]], List[Dict[str, Any]]]:

        self._set_schema()
        successes: List[Tuple[OrganisationSource, str]] = []
        errors: List[Dict[str, Any]] = []

        for idx, file in enumerate(payload.files):
            source_id = uuid4()
            blob_path = self._plan_blob_path(user_id, source_id, file.filename)
            try:
                upload_url = await self.storage.generate_write_sas("attachments", blob_path, minutes=15)
            except Exception as e:
                logger.error("Failed generating write SAS for source %s: %s", source_id, e, exc_info=True)
                errors.append({
                    "index": idx, 
                    "filename": file.filename, 
                    "source_id": str(source_id),
                    "reason": "signing_failed",
                    "error_message": f"Failed to generate upload URL: {str(e)}"
                })
                continue

            source = OrganisationSource(
                source_id=source_id,
                organisation_id=self.organisation_id,
                user_id=user_id,
                filename=file.filename,
                storage_path=blob_path,
                mimetype=file.mimetype,
                size_bytes=file.size_bytes,
                status=OrganisationSourceStatus.UPLOADED,
                filters=payload.default_filters or {},
            )
            try:
                self.org_session.add(source)
                self.org_session.commit()
                self.org_session.refresh(source)
                successes.append((source, upload_url))
            except Exception as e:
                self.org_session.rollback()
                logger.error("Failed to create organisation source %s: %s", source_id, e, exc_info=True)
                errors.append({
                    "index": idx, 
                    "filename": file.filename, 
                    "source_id": str(source_id),
                    "reason": "db_error",
                    "error_message": f"Failed to save source to database: {str(e)}"
                })

        return successes, errors

    async def finalize_source(self, request: KnowledgeFinalizeRequest, user_id: UUID) -> OrganisationSource:
        self._set_schema()
        source: OrganisationSource | None = self.org_session.get(OrganisationSource, request.source_id)
        if (
            not source
            or source.organisation_id != self.organisation_id
            or source.user_id != user_id
        ):
            raise KnowledgeUploadServiceError("Source not found or access denied", status_code=404)

        # Normalize status to enum if serialized as plain string by SQLModel
        if isinstance(source.status, str):
            try:
                source.status = OrganisationSourceStatus(source.status)  # type: ignore
            except ValueError:
                logger.warning("Unknown status string '%s' for source %s", source.status, source.source_id)

        if source.status not in [OrganisationSourceStatus.UPLOADED, OrganisationSourceStatus.PROCESSING]:
            return source

        try:
            props = await self.storage.get_blob_properties("attachments", source.storage_path)
            if not props:
                raise KnowledgeUploadServiceError(
                    "Uploaded file not yet available. Retry later.", status_code=404
                )
            actual_size = props.get("size")
            if actual_size is None:
                raise KnowledgeUploadServiceError("Missing blob size", status_code=500)
            if source.size_bytes and actual_size != source.size_bytes:
                logger.error(
                    "Size mismatch for source %s: reported %s vs actual %s", source.source_id, source.size_bytes, actual_size
                )
                source.status = OrganisationSourceStatus.FAILED
                source.error_message = "size_mismatch"
                self.org_session.add(source)
                self.org_session.commit()
                return source
            source.status = OrganisationSourceStatus.PROCESSING
            if request.checksum:
                # Store checksum inside filters (no meta_data field on OrganisationSource)
                source.filters = (source.filters or {})
                source.filters["_client_checksum"] = request.checksum
            self.org_session.add(source)
            self.org_session.commit()
            self.org_session.refresh(source)
        except KnowledgeUploadServiceError:
            raise
        except Exception as e:
            self.org_session.rollback()
            logger.error("Failed during finalize for source %s: %s", request.source_id, e, exc_info=True)
            raise KnowledgeUploadServiceError("Failed to finalize source") from e

        return source

    async def get_source(self, source_id: UUID, user_id: UUID) -> OrganisationSource:
        self._set_schema()
        source = self.org_session.get(OrganisationSource, source_id)
        if (
            not source
            or source.organisation_id != self.organisation_id
            or source.user_id != user_id
        ):
            raise KnowledgeUploadServiceError("Source not found or access denied", status_code=404)
        self.org_session.refresh(source)
        return source
