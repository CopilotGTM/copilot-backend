"""Service layer for knowledge source uploads.

Responsibilities:
    * Generate presigned (SAS) upload URLs for new knowledge sources.
    * Persist initial source records (initial status=PENDING_UPLOAD).
    * Finalize a source after client upload (verify blob -> UPLOADED -> PROCESSING).
    * Provide accessors for retrieving source state.

Schema handling:
    The per‑organisation schema (`search_path`) is applied centrally by
    `get_org_session` in `infra.utils.database`. This service therefore
    MUST NOT attempt to re-apply or mutate the search_path; doing so
    would duplicate logic and risk divergence. Any code that needs an
    organisation context should obtain the session *only* through the
    dependency / helper, guaranteeing the correct schema.

The heavier post‑upload processing (checksum / thumbnail generation) is
delegated to a Celery task to keep API latency low.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple
from uuid import UUID, uuid4

from sqlmodel import Session

from src.api.schemas.knowledge_upload_models import (
    KnowledgeBatchPresignRequest,
    KnowledgeFinalizeRequest,
)
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class KnowledgeUploadServiceError(Exception):
    """Domain exception carrying an HTTP status code for API translation."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class KnowledgeUploadService:
    def __init__(self, org_session: Session, organisation_id: UUID):
        """Bind service to a specific organisation session & storage context.

        Assumes the passed session already has the correct search_path applied
        (enforced by get_org_session). This class must not change search_path.
        """
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)

    # Backwards‑compat: some routers referenced `service.session` (incorrect name).
    # Provide a property delegating to the canonical `org_session` to avoid AttributeError 500s.
    @property
    def session(self) -> Session:  # pragma: no cover - thin delegation
        return self.org_session

    async def close(self):  # pragma: no cover - simple resource cleanup
        """Release any async resources (Azure clients)."""
        try:
            await self.storage.close()
        except Exception as exc:  # Log & swallow to not mask response lifecycle
            logger.debug("KnowledgeUploadService.close swallow error: %s", exc)

    def _plan_blob_path(self, user_id: UUID, source_id: UUID, filename: str) -> str:
        """Return canonical blob path for a source.

        Pattern: knowledgebase/{user_id}/{source_id}/{sanitized_filename}
        """
        base = os.path.basename(filename).strip().replace(" ", "_")
        # Keep a conservative character set to avoid escaping issues.
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "file"
        return f"knowledgebase/{user_id}/{source_id}/{base}"

    async def presign_batch(
        self,
        payload: KnowledgeBatchPresignRequest,
        user_id: UUID,
        expiry_time: int = 15
    ) -> Tuple[List[Tuple[OrganisationSource, str]], List[Dict[str, Any]]]:
        """Create records and return upload SAS URLs for each requested file.

        Returns a tuple: (successes, errors). Each success entry is
        `(OrganisationSource, upload_url)`.
        """
        successes: List[Tuple[OrganisationSource, str]] = []
        errors: List[Dict[str, Any]] = []

        for idx, file in enumerate(payload.files):
            source_id = uuid4()
            blob_path = self._plan_blob_path(user_id, source_id, file.filename)
            try:
                upload_url = await self.storage.generate_write_sas(blob_path, minutes=expiry_time)
            except Exception as exc:  # Broad: network / credential / azure errors
                logger.error(
                    "Failed generating write SAS for source %s: %s", source_id, exc, exc_info=True
                )
                errors.append(
                    {
                        "index": idx,
                        "filename": file.filename,
                        "source_id": str(source_id),
                        "reason": "signing_failed",
                        "error_message": f"Failed to generate upload URL: {exc}",
                    }
                )
                continue

            source = OrganisationSource(
                source_id=source_id,
                organisation_id=self.organisation_id,
                user_id=user_id,
                filename=file.filename,
                storage_path=blob_path,
                mimetype=file.mimetype,
                size_bytes=file.size_bytes,
                status=OrganisationSourceStatus.PENDING_UPLOAD,
            )

            try:
                self.org_session.add(source)
                self.org_session.commit()
                self.org_session.refresh(source)
                successes.append((source, upload_url))
            except Exception as exc:  # Broad DB failure capture
                self.org_session.rollback()
                logger.error(
                    "Failed to create organisation source %s: %s", source_id, exc, exc_info=True
                )
                errors.append(
                    {
                        "index": idx,
                        "filename": file.filename,
                        "source_id": str(source_id),
                        "reason": "db_error",
                        "error_message": f"Failed to save source to database: {exc}",
                    }
                )

        return successes, errors

    async def finalize_source(
        self, request: KnowledgeFinalizeRequest, user_id: UUID
    ) -> OrganisationSource:
        """Validate uploaded blob presence then mark source for processing.

        Light validation only (size mismatch intentionally skipped by
        product decision). A missing blob yields a 404 encouraging the
        client to retry later.
        """

        # Retrieve the source from the database
        source: OrganisationSource | None = self.org_session.get(
            OrganisationSource, 
            request.source_id
        )

        # Check for ownership and existence
        if (
            not source
            or source.organisation_id != self.organisation_id
            or source.user_id != user_id
        ):
            raise KnowledgeUploadServiceError(
                "Source not found or access denied", status_code=404
            )

        # Normalize status to enum if deserialized as plain string
        if isinstance(source.status, str):
            try:
                source.status = OrganisationSourceStatus(source.status)  # type: ignore[assignment]
            except ValueError:
                logger.warning(
                    "Unknown status string '%s' for source %s",
                    source.status,
                    source.source_id,
                )

        if source.status != OrganisationSourceStatus.PENDING_UPLOAD:
            return source

        try:
            props = await self.storage.get_blob_properties(source.storage_path)
            if not props:
                raise KnowledgeUploadServiceError(
                    "Uploaded file not yet available. Retry later.", status_code=404
                )

            # These will be used in future
            actual_size = props.get("size")
            if actual_size is None:
                raise KnowledgeUploadServiceError("Missing blob size", status_code=500)

            # Persist client-provided checksum into single `checksum` field.
            if request.checksum:
                source.checksum = request.checksum

            source.status = OrganisationSourceStatus.PROCESSING
            self.org_session.add(source)
            self.org_session.commit()
            self.org_session.refresh(source)
        except KnowledgeUploadServiceError:
            raise
        except Exception as exc:  # Broad catch for storage/DB errors
            self.org_session.rollback()
            logger.error(
                "Failed during finalize for source %s: %s", request.source_id, exc, exc_info=True
            )
            raise KnowledgeUploadServiceError("Failed to finalize source") from exc

        return source

    async def get_source(self, source_id: UUID, user_id: UUID) -> OrganisationSource:
        """Retrieve a source (permission checked) from the org schema."""
        source = self.org_session.get(OrganisationSource, source_id)
        if (
            not source
            or source.organisation_id != self.organisation_id
            or source.user_id != user_id
        ):
            raise KnowledgeUploadServiceError(
                "Source not found or access denied", status_code=404
            )
        self.org_session.refresh(source)
        return source
