"""Audited CSV import orchestration from upload analysis through row results."""

from __future__ import annotations

import csv
import inspect
import io
import json
import math
import re
import unicodedata
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStage, CRMPipeline
from src.domain.models.organisation_tables.sync.crm.imports import (
    CRMImportRow,
    CRMImportRowStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteServiceError,
)
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.imports.csv_mapping import suggest_csv_mappings
from src.service.crm.imports.csv_parser import (
    MAX_CSV_FILE_SIZE_BYTES,
    iter_crm_csv_rows,
    parse_crm_csv_content,
    sanitize_csv_cell_for_export,
)
from src.service.crm.internal.internal_crm_setup_service import (
    AUTOMATED_PIPELINE_USER_ID,
    InternalCRMSetupService,
)
from src.service.crm.schema.deal_schema_service import DealSchemaService
from src.service.crm.utils.account import (
    ACCOUNT_LINKEDIN_URL_ERROR,
    PERSON_LINKEDIN_URL_ERROR,
    is_account_linkedin_url,
    is_person_linkedin_url,
    normalize_account_domain,
    normalize_company_name,
    normalize_linkedin_url,
)
from src.service.crm.utils.identity import is_valid_email, normalize_email
from src.service.crm.utils.pipeline_shape import (
    has_required_pipeline_shape,
    required_pipeline_shape_message,
)

IMPORT_BATCH_SIZE = 250
_ROW_AUDIT_KEY = "_audit"
ACCOUNT_IDENTITY_FIELDS = {
    "account.name",
    "account.domain",
    "account.website",
    "account.linkedin_url",
}
CONTACT_IDENTITY_FIELDS = {"contact.email", "contact.linkedin_url"}

_STANDARD_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("account.name", "Account name", "account", "text"),
    ("account.domain", "Account domain", "account", "url"),
    ("account.website", "Account website", "account", "url"),
    ("account.industry", "Account industry", "account", "text"),
    ("account.phone", "Account phone", "account", "text"),
    ("account.city", "Account city", "account", "text"),
    ("account.state", "Account state", "account", "text"),
    ("account.country", "Account country", "account", "text"),
    ("account.address", "Account address", "account", "text"),
    ("account.postal_code", "Account postal code", "account", "text"),
    ("account.linkedin_url", "Account LinkedIn URL", "account", "url"),
    ("contact.name", "Contact name", "contact", "text"),
    ("contact.first_name", "Contact first name", "contact", "text"),
    ("contact.last_name", "Contact last name", "contact", "text"),
    ("contact.email", "Contact email", "contact", "email"),
    ("contact.job_title", "Contact job title", "contact", "text"),
    ("contact.phone", "Contact phone", "contact", "text"),
    ("contact.mobile_phone", "Contact mobile phone", "contact", "text"),
    ("contact.website", "Contact website", "contact", "url"),
    ("contact.address", "Contact address", "contact", "text"),
    ("contact.city", "Contact city", "contact", "text"),
    ("contact.state", "Contact state", "contact", "text"),
    ("contact.country", "Contact country", "contact", "text"),
    ("contact.postal_code", "Contact postal code", "contact", "text"),
    ("contact.linkedin_url", "Contact LinkedIn URL", "contact", "url"),
    ("deal.name", "Deal name", "deal", "text"),
    ("deal.amount", "Deal amount", "deal", "currency"),
    ("deal.currency", "Deal currency", "deal", "text"),
    ("deal.source_created_at", "Deal created date", "deal", "datetime"),
    ("deal.expected_close_date", "Expected close date", "deal", "date"),
    ("deal.description", "Deal description", "deal", "text"),
    ("deal.deal_type", "Deal type", "deal", "text"),
    ("deal.priority", "Deal priority", "deal", "text"),
    ("deal.lead_source", "Deal lead source", "deal", "text"),
    ("deal.probability", "Deal probability", "deal", "percentage"),
    ("deal.forecast_category", "Forecast category", "deal", "text"),
    ("deal.owner", "Deal owner", "deal", "owner"),
    ("deal.stage", "Deal stage", "deal", "stage"),
)


@dataclass(frozen=True)
class CRMImportPresignResult:
    """Import identity and scoped blob upload details."""

    import_id: UUID
    upload_url: str
    storage_path: str


@dataclass(frozen=True)
class CRMImportJobResult:
    """Service result containing the persisted parent job."""

    job: DealJobs


@dataclass(frozen=True)
class CRMImportRowsResult:
    """Cursor page of persisted import rows."""

    items: list[CRMImportRow]
    next_cursor: int | None
    has_more: bool


@dataclass(frozen=True)
class CRMImportTargetsResult:
    """Writable target catalog and stages for one internal pipeline."""

    pipeline: CRMPipeline
    stages: list[CRMDealStage]
    targets: list[dict[str, Any]]


@dataclass(frozen=True)
class CRMImportBatchResult:
    """Committed batch state used by the worker to schedule continuation."""

    job: DealJobs
    has_more: bool = False
    lock_acquired: bool = True


class CRMImportServiceError(Exception):
    """Expected API or row validation failure with a safe client message."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class CRMImportService:
    """Own import configuration, deterministic resolution, and durable progress."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None,
        *,
        storage: AzureOrganisationBlobStorage | None = None,
        task_sender=None,
        master_session: AsyncSession | None = None,
    ):
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._current_actor_user_id = user_id
        self.storage = storage or AzureOrganisationBlobStorage(organisation_id)
        self.task_sender = task_sender
        self._stage_cache: dict[UUID, list[CRMDealStage]] = {}
        self._target_type_cache: dict[UUID, dict[str, str]] = {}
        self._org_users_cache: list[User] | None = None

    async def presign(
        self,
        original_filename: str,
        *,
        mode: str = "account_contact_deal",
        pipeline_id: UUID,
        default_stage_id: UUID,
        default_owner_user_id: UUID | None = None,
        options: dict[str, Any] | None = None,
    ) -> CRMImportPresignResult:
        """Create an upload job only after its deal configuration is usable."""
        if self.user_id is None:
            raise CRMImportServiceError("User id is required to create an import", 400)
        if mode != "account_contact_deal":
            raise CRMImportServiceError("Only account_contact_deal imports are supported", 400)
        if options and options.get("update_existing"):
            raise CRMImportServiceError("update_existing is not supported in V1", 400)
        await self._validate_configuration(
            pipeline_id=pipeline_id,
            default_stage_id=default_stage_id,
            default_owner_user_id=default_owner_user_id,
        )
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            deal_id=None,
            job_type=DealJobType.CRM_CSV_IMPORT,
            status=JobStatus.PENDING,
            job_metadata={},
        )
        storage_path = self.storage.plan_crm_import_path(
            user_id=self.user_id,
            import_id=job.job_id,
            original_filename=original_filename,
        )
        job.job_metadata = {
            "filename": original_filename,
            "storage_path": storage_path,
            "mode": mode,
            "options": options or {},
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(default_stage_id),
            "default_owner_user_id": (
                str(default_owner_user_id) if default_owner_user_id else None
            ),
            "ignored_columns": [],
            "column_formats": {},
            "crm_import_status": "uploading",
            "presigned_at": _now_iso(),
        }
        self.org_session.add(job)
        upload_url = await self.storage.generate_write_sas(storage_path)
        await self._commit()
        return CRMImportPresignResult(job.job_id, upload_url, storage_path)

    async def get_targets(self, pipeline_id: UUID) -> CRMImportTargetsResult:
        """Return the single backend-owned mapping catalog for one writable pipeline."""
        pipeline, stages = await self._load_import_pipeline(pipeline_id)
        targets: list[dict[str, Any]] = [
            {
                "target_field": target_field,
                "label": label,
                "group": group,
                "value_type": value_type,
                "required": target_field == "deal.name",
                "read_only": False,
                "custom_field_id": None,
                "custom_field_key": None,
                "options": {},
                "validation": {},
            }
            for target_field, label, group, value_type in _STANDARD_TARGETS
        ]
        schema = await DealSchemaService(self.org_session, self.organisation_id).get_schema()
        for field in schema.fields:
            if field.source != "internal" or field.read_only or not field.internal_write:
                continue
            targets.append(
                {
                    "target_field": f"deal.custom.{field.key}",
                    "label": field.label,
                    "group": "deal",
                    "value_type": field.type,
                    "required": field.required,
                    "read_only": False,
                    "custom_field_id": field.field_id,
                    "custom_field_key": field.key,
                    "options": field.options,
                    "validation": field.validation,
                }
            )
        return CRMImportTargetsResult(pipeline=pipeline, stages=stages, targets=targets)

    async def finalize(self, import_id: UUID) -> CRMImportJobResult:
        """Verify the uploaded blob and transition its job to analysis."""
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        import_status = metadata.get("crm_import_status")
        if import_status in {"analyzing", "ready_for_mapping", "importing", "completed"}:
            # Queue publication happens after this commit. Returning the stored
            # job lets an HTTP retry republish analysis without regressing state.
            return CRMImportJobResult(job)
        if job.status != JobStatus.PENDING:
            raise CRMImportServiceError("Import cannot be finalized in its current state", 409)
        storage_path = metadata.get("storage_path")
        if not storage_path:
            raise CRMImportServiceError("Import is missing its storage path", 400)
        props = await self.storage.get_blob_properties(storage_path)
        if not props:
            raise CRMImportServiceError("Uploaded CSV was not found", 404)
        size = int(props.get("size") or 0)
        if size > MAX_CSV_FILE_SIZE_BYTES:
            raise CRMImportServiceError("CSV file exceeds the 10 MB limit", 400)
        metadata.update(
            {
                "crm_import_status": "analyzing",
                "file_size_bytes": size,
                "finalized_at": _now_iso(),
            }
        )
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def analyze(self, import_id: UUID) -> CRMImportJobResult:
        """Parse all bounded rows and persist deterministic mapping suggestions."""
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        import_status = metadata.get("crm_import_status")
        if import_status in {"ready_for_mapping", "importing", "completed", "failed"}:
            return CRMImportJobResult(job)
        if import_status != "analyzing":
            raise CRMImportServiceError("Import is not ready for analysis", 409)
        if not await self._try_acquire_job_lock(import_id, operation="analyze"):
            return CRMImportJobResult(job)
        storage_path = metadata.get("storage_path")
        if not storage_path:
            raise CRMImportServiceError("Import is missing its storage path", 400)

        content = await self.storage.get_blob_content(storage_path)
        parsed = parse_crm_csv_content(content)
        if parsed.blocking_errors:
            job.status = JobStatus.FAILED
            job.error_message = parsed.blocking_errors[0].message
            metadata["crm_import_status"] = "failed"
            metadata["parse_errors"] = [asdict(error) for error in parsed.blocking_errors]
            job.job_metadata = metadata
            await self._commit()
            return CRMImportJobResult(job)

        pipeline_id = _metadata_uuid(metadata, "pipeline_id")
        if pipeline_id is None:
            raise CRMImportServiceError("Import is missing its pipeline configuration", 400)
        catalog = await self.get_targets(pipeline_id)
        suggestions = suggest_csv_mappings(
            parsed.headers,
            parsed.sample_rows[:20],
            targets=catalog.targets,
            stage_values={
                _normalize_match_value(value)
                for stage in catalog.stages
                for value in (stage.name, stage.label, stage.stage_key)
                if value
            },
        )
        existing_rows = await self._load_rows(import_id)
        existing_numbers = {row.row_number for row in existing_rows}
        ambiguous_date_headers: set[str] = set()
        for row_number, raw_values in enumerate(
            iter_crm_csv_rows(content, parsed.headers), start=1
        ):
            ambiguous_date_headers.update(
                header
                for header, value in raw_values.items()
                if _is_ambiguous_numeric_date(str(value))
            )
            if row_number in existing_numbers:
                continue
            self.org_session.add(
                CRMImportRow(
                    import_job_id=job.job_id,
                    organisation_id=self.organisation_id,
                    row_number=row_number,
                    raw_values=raw_values,
                    normalized_values={},
                    status=CRMImportRowStatus.PREVIEW,
                )
            )
            if row_number % IMPORT_BATCH_SIZE == 0:
                # Flushing releases pending ORM row objects while the enclosing
                # analysis transaction still keeps the state change atomic.
                await self.org_session.flush()
        metadata.update(
            {
                "crm_import_status": "ready_for_mapping",
                "analysis": {
                    "headers": parsed.headers,
                    "total_rows": parsed.total_rows,
                    "sampled_rows": len(parsed.sample_rows),
                    "sample_rows": parsed.sample_rows,
                    "ambiguous_numeric_date_headers": sorted(ambiguous_date_headers),
                },
                "mapping_suggestions": [asdict(suggestion) for suggestion in suggestions],
                "analyzed_at": _now_iso(),
            }
        )
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def save_mapping(
        self,
        import_id: UUID,
        mapping: dict[str, str],
        *,
        ignored_columns: list[str] | None = None,
        column_formats: dict[str, str] | None = None,
        pipeline_id: UUID | None = None,
        default_stage_id: UUID | None = None,
        default_owner_user_id: UUID | None = None,
        retry_failed_rows: bool = False,
        options: dict[str, Any] | None = None,
    ) -> CRMImportJobResult:
        """Validate and persist the complete mapping draft, optionally preparing failed rows."""
        duplicate_targets = sorted(
            target for target, count in Counter(mapping.values()).items() if count > 1
        )
        if duplicate_targets:
            raise CRMImportServiceError(
                "Duplicate CRM import target field",
                400,
                {"duplicate_target_fields": duplicate_targets},
            )
        if options and options.get("update_existing"):
            raise CRMImportServiceError("update_existing is not supported in V1", 400)
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        await self._ensure_mapping_can_be_saved(job, metadata)
        resolved_pipeline_id = pipeline_id or _metadata_uuid(metadata, "pipeline_id")
        resolved_stage_id = default_stage_id or _metadata_uuid(metadata, "default_stage_id")
        if resolved_pipeline_id is None or resolved_stage_id is None:
            raise CRMImportServiceError("Pipeline and default stage are required", 400)
        await self._validate_configuration(
            pipeline_id=resolved_pipeline_id,
            default_stage_id=resolved_stage_id,
            default_owner_user_id=default_owner_user_id,
        )
        ignored = ignored_columns or []
        formats = column_formats or {}
        await self._validate_mapping_draft(
            metadata=metadata,
            mapping=mapping,
            ignored_columns=ignored,
            column_formats=formats,
            pipeline_id=resolved_pipeline_id,
        )
        metadata["mapping"] = mapping
        metadata["options"] = options or {}
        metadata["ignored_columns"] = ignored
        metadata["column_formats"] = formats
        metadata["pipeline_id"] = str(resolved_pipeline_id)
        metadata["default_stage_id"] = str(resolved_stage_id)
        metadata["default_owner_user_id"] = (
            str(default_owner_user_id) if default_owner_user_id else None
        )
        if retry_failed_rows:
            await self._prepare_failed_rows_for_retry(job, metadata)
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def start(self, import_id: UUID) -> CRMImportJobResult:
        """Revalidate stored configuration and dispatch the first durable batch."""
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        if job.status == JobStatus.IN_PROGRESS and metadata.get("crm_import_status") == "importing":
            await self._send_process_task(job.job_id)
            return CRMImportJobResult(job)
        await self._ensure_import_can_start(job, metadata)
        pipeline_id = _metadata_uuid(metadata, "pipeline_id")
        default_stage_id = _metadata_uuid(metadata, "default_stage_id")
        if pipeline_id is None or default_stage_id is None:
            raise CRMImportServiceError("Pipeline and default stage are required", 400)
        await self._validate_configuration(
            pipeline_id=pipeline_id,
            default_stage_id=default_stage_id,
            default_owner_user_id=_metadata_uuid(metadata, "default_owner_user_id"),
        )
        await self._validate_mapping_draft(
            metadata=metadata,
            mapping=metadata.get("mapping") or {},
            ignored_columns=metadata.get("ignored_columns") or [],
            column_formats=metadata.get("column_formats") or {},
            pipeline_id=pipeline_id,
        )

        job.status = JobStatus.IN_PROGRESS
        metadata["crm_import_status"] = "importing"
        metadata["started_at"] = _now_iso()
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        await self._send_process_task(job.job_id)
        return CRMImportJobResult(job)

    async def process_next_batch(self, import_id: UUID) -> CRMImportBatchResult:
        """Process at most 250 ready rows and commit one retry-safe batch."""
        if not await self._try_acquire_import_lock(import_id):
            # Duplicate Celery deliveries serialize at the database boundary;
            # the lock owner is responsible for dispatching the continuation.
            job = await self._load_job(import_id)
            return CRMImportBatchResult(job=job, lock_acquired=False)

        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        if metadata.get("crm_import_status") != "importing":
            # A stale or premature delivery must not advance or regress the
            # user-controlled import state machine.
            return CRMImportBatchResult(job=job, lock_acquired=False)
        mapping = metadata.get("mapping") or {}
        if not mapping:
            raise CRMImportServiceError("Import mapping has not been saved", 409)
        pipeline_id = _metadata_uuid(metadata, "pipeline_id")
        default_stage_id = _metadata_uuid(metadata, "default_stage_id")
        if pipeline_id is None or default_stage_id is None:
            raise CRMImportServiceError("Pipeline and default stage are required", 400)
        await self._validate_configuration(
            pipeline_id=pipeline_id,
            default_stage_id=default_stage_id,
            default_owner_user_id=_metadata_uuid(metadata, "default_owner_user_id"),
        )

        rows = await self._load_ready_batch(import_id)
        if not rows:
            if metadata.get("crm_import_status") == "importing":
                job.status = JobStatus.COMPLETED
                metadata["crm_import_status"] = "completed"
                metadata["completed_at"] = _now_iso()
                job.job_metadata = metadata
                self.org_session.add(job)
                await self._commit()
            return CRMImportBatchResult(job=job)

        account_writer = AccountWriteService(
            self.org_session, self.organisation_id, self.master_session
        )
        contact_writer = ContactWriteService(
            self.org_session, self.organisation_id, self.master_session
        )
        for row in rows:
            attempt_count = int(_row_audit(row).get("attempt_count") or 0) + 1
            row.normalized_values = {
                target: str(row.raw_values.get(header) or "").strip()
                for header, target in mapping.items()
            }
            _update_row_audit(row, attempt_count=attempt_count)
            try:
                prepared = await self._prepare_row(row, metadata)
                row.normalized_values = prepared["normalized_values"]
                _update_row_audit(
                    row,
                    attempt_count=attempt_count,
                    owner_user_id=prepared["owner_user_id"],
                    stage_id=prepared["stage_id"],
                )
                async with self._row_savepoint():
                    await self._acquire_identity_locks(prepared)
                    account_result = await self._process_account(row, mapping, account_writer)
                    contact_result = await self._process_contact(
                        row,
                        mapping,
                        contact_writer,
                        account_id=account_result.account.company_id,
                    )
                    deal_id, _deal_created = await self._process_deal(
                        row,
                        mapping,
                        account_id=account_result.account.company_id,
                        contact_id=contact_result.contact.contact_id,
                        stage_id=prepared["stage_id"],
                        owner_user_id=prepared["owner_user_id"],
                        custom_properties=prepared["custom_properties"],
                        metadata=metadata,
                    )
                row.account_id = account_result.account.company_id
                row.contact_id = contact_result.contact.contact_id
                row.deal_id = deal_id
                prepared["normalized_values"].update(
                    {
                        "resolved.account_name": account_result.account.name,
                        "resolved.contact_name": getattr(contact_result.contact, "full_name", None),
                        "resolved.contact_email": getattr(contact_result.contact, "email", None),
                    }
                )
                public_values = prepared["normalized_values"]
                row.normalized_values = public_values
                _update_row_audit(
                    row,
                    attempt_count=attempt_count,
                    owner_user_id=prepared["owner_user_id"],
                    stage_id=prepared["stage_id"],
                    account_outcome="created" if account_result.created else "matched",
                    contact_outcome="created" if contact_result.created else "matched",
                    error_field=None,
                )
                row.target_type = "deal"
                row.status = CRMImportRowStatus.CREATED
                row.error_code = None
                row.error_message = None
            except (
                CRMImportServiceError,
                AccountWriteServiceError,
                ContactWriteServiceError,
                DealWriteServiceError,
            ) as exc:
                row.status = CRMImportRowStatus.FAILED
                row.error_code = _row_error_code(exc)
                _update_row_audit(row, error_field=_row_error_field(exc))
                row.error_message = exc.message
            self.org_session.add(row)

        await self.org_session.flush()
        has_more = await self._has_ready_rows(import_id)
        job.result_data = _increment_counts(
            job.result_data or {},
            rows,
            total_rows=int(
                (metadata.get("analysis") or {}).get("total_rows")
                or (job.result_data or {}).get("total_rows")
                or len(rows)
            ),
        )
        if has_more:
            job.status = JobStatus.IN_PROGRESS
            metadata["crm_import_status"] = "importing"
        else:
            # Row failures are a completed audited outcome, not a fatal worker failure.
            job.status = JobStatus.COMPLETED
            metadata["crm_import_status"] = "completed"
            metadata["completed_at"] = _now_iso()
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        return CRMImportBatchResult(job=job, has_more=has_more)

    async def get_status(self, import_id: UUID) -> CRMImportJobResult:
        """Load the caller-scoped parent import job."""
        return CRMImportJobResult(await self._load_job(import_id))

    async def list_rows(
        self,
        import_id: UUID,
        *,
        cursor: int | None = None,
        limit: int = 100,
        status: str | None = None,
        q: str | None = None,
    ) -> CRMImportRowsResult:
        """Page row outcomes using SQL filters in production sessions."""
        await self._load_job(import_id)
        if hasattr(self.org_session, "bind"):
            filters = [
                col(CRMImportRow.organisation_id) == self.organisation_id,
                col(CRMImportRow.import_job_id) == import_id,
            ]
            if status:
                filters.append(col(CRMImportRow.status) == status)
            if cursor is not None:
                filters.append(col(CRMImportRow.row_number) > cursor)
            if q:
                escaped_q = _escape_like(q)
                filters.append(
                    cast(CRMImportRow.normalized_values, String).ilike(
                        f"%{escaped_q}%", escape="\\"
                    )
                )
            stmt = (
                select(CRMImportRow)
                .where(*filters)
                .order_by(col(CRMImportRow.row_number).asc())
                .limit(limit + 1)
            )
            result = await self.org_session.execute(stmt)
            rows = list(result.scalars().all())
            page = rows[:limit]
            return CRMImportRowsResult(
                items=page,
                next_cursor=page[-1].row_number if page else None,
                has_more=len(rows) > limit,
            )
        rows = sorted(await self._load_rows(import_id), key=lambda row: row.row_number)
        if status:
            rows = [row for row in rows if _enum_value(row.status) == status]
        if cursor is not None:
            rows = [row for row in rows if row.row_number > cursor]
        if q:
            lowered = q.lower()
            rows = [row for row in rows if lowered in str(row.normalized_values).lower()]

        # copilotgtm: V1 import size is capped at 25k rows, so in-memory row
        # paging is acceptable; move this to SQL cursor queries if the cap grows.
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = page[-1].row_number if page else None
        return CRMImportRowsResult(items=page, next_cursor=next_cursor, has_more=has_more)

    async def build_error_csv(self, import_id: UUID) -> str:
        """Export unsuccessful rows with original columns and safe spreadsheet cells."""
        job = await self._load_job(import_id)
        rows = sorted(await self._load_rows(import_id), key=lambda row: row.row_number)
        error_rows = [row for row in rows if row.error_code or row.error_message]
        headers = list(((job.job_metadata or {}).get("analysis") or {}).get("headers") or [])
        if not headers:
            headers = list(dict.fromkeys(header for row in error_rows for header in row.raw_values))

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            ["row_number", "status", "error_code", "error_field", "error_message", *headers]
        )
        for row in error_rows:
            writer.writerow(
                [
                    sanitize_csv_cell_for_export(row.row_number),
                    sanitize_csv_cell_for_export(_enum_value(row.status)),
                    sanitize_csv_cell_for_export(row.error_code),
                    sanitize_csv_cell_for_export(_row_audit(row).get("error_field")),
                    sanitize_csv_cell_for_export(row.error_message),
                    *[
                        sanitize_csv_cell_for_export(row.raw_values.get(header))
                        for header in headers
                    ],
                ]
            )
        return output.getvalue()

    async def _process_account(
        self,
        row: CRMImportRow,
        mapping: dict[str, str],
        account_writer: AccountWriteService,
    ):
        values = _values_for_prefix(row.raw_values, mapping, "account.")
        command = AccountCreateCommand(
            name=values.get("name"),
            domain=values.get("domain"),
            website=values.get("website"),
            industry=values.get("industry"),
            phone=values.get("phone"),
            city=values.get("city"),
            state=values.get("state"),
            country=values.get("country"),
            address=values.get("address"),
            postal_code=values.get("postal_code"),
            linkedin_url=values.get("linkedin_url"),
        )
        context = AccountWriteContext(
            source="crm_import",
            actor_user_id=self._current_actor_user_id,
            created_via="crm_import",
        )
        resolver = getattr(account_writer, "resolve_or_create_local_account", None)
        if resolver is not None:
            return await resolver(command, context=context)
        return await account_writer.create_account(command, context=context)

    async def _process_contact(
        self,
        row: CRMImportRow,
        mapping: dict[str, str],
        contact_writer: ContactWriteService,
        *,
        account_id: UUID | None,
    ):
        values = _values_for_prefix(row.raw_values, mapping, "contact.")
        name = values.get("name") or " ".join(
            value for value in [values.get("first_name"), values.get("last_name")] if value
        )
        command = ContactCreateCommand(
            email=values.get("email"),
            name=name or None,
            job_title=values.get("job_title"),
            phone=values.get("phone"),
            mobile_phone=values.get("mobile_phone"),
            website=values.get("website"),
            address=values.get("address"),
            city=values.get("city"),
            state=values.get("state"),
            country=values.get("country"),
            postal_code=values.get("postal_code"),
            linkedin_url=values.get("linkedin_url"),
            company_id=account_id,
        )
        context = ContactWriteContext(
            source="crm_import",
            actor_user_id=self._current_actor_user_id,
        )
        resolver = getattr(contact_writer, "resolve_or_create_local_contact", None)
        if resolver is not None:
            return await resolver(command, context=context)
        return await contact_writer.create_contact(command, context=context)

    async def _process_deal(
        self,
        row: CRMImportRow,
        mapping: dict[str, str],
        *,
        account_id: UUID,
        contact_id: UUID,
        stage_id: UUID,
        owner_user_id: UUID | None,
        custom_properties: dict[str, Any],
        metadata: dict[str, Any],
    ) -> tuple[UUID | None, bool]:
        values = _values_for_prefix(row.raw_values, mapping, "deal.")
        if self.master_session is None:
            raise DealWriteServiceError(
                "Master session is required to import deals", status_code=500
            )
        if self._current_actor_user_id is None:
            raise DealWriteServiceError("User id is required to import deals", status_code=400)

        payload = _deal_payload_from_values(
            values,
            column_formats=metadata.get("column_formats") or {},
            mapping=mapping,
        )
        payload.update(
            {
                "pipeline_id": _metadata_uuid(metadata, "pipeline_id"),
                "stage_id": stage_id,
                "account": {"company_id": account_id},
                "contact_participants": [{"contact_id": contact_id}],
                "custom_properties": custom_properties,
                "created_via": "crm_import",
            }
        )
        if owner_user_id:
            payload["owner_user_id"] = owner_user_id

        writer = DealWriteService(
            self.org_session,
            self.master_session,
            self.organisation_id,
            self._current_actor_user_id,
        )
        deal = await writer.create_deal(
            payload,
            context=DealWriteContext(
                source="import_assistant",
                actor_user_id=self._current_actor_user_id,
                source_label="crm_import",
            ),
        )
        return deal.deal_id, True

    async def _load_job(self, import_id: UUID) -> DealJobs:
        filters = [
            col(DealJobs.job_id) == import_id,
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.job_type) == DealJobType.CRM_CSV_IMPORT,
        ]
        if self.user_id is not None:
            filters.append(col(DealJobs.user_id) == self.user_id)
        stmt = select(DealJobs).where(*filters)
        result = await self.org_session.execute(stmt)
        job = result.scalars().first()
        if not job:
            raise CRMImportServiceError("CRM import job not found", 404)
        return job

    async def _load_rows(self, import_id: UUID) -> list[CRMImportRow]:
        stmt = select(CRMImportRow).where(
            col(CRMImportRow.organisation_id) == self.organisation_id,
            col(CRMImportRow.import_job_id) == import_id,
        )
        result = await self.org_session.execute(stmt)
        return list(result.scalars().all())

    async def _try_acquire_import_lock(self, import_id: UUID) -> bool:
        """Serialize duplicate worker deliveries for one import transaction."""
        return await self._try_acquire_job_lock(import_id, operation="process")

    async def _try_acquire_job_lock(self, import_id: UUID, *, operation: str) -> bool:
        """Claim one import operation until the current transaction ends."""
        if not hasattr(self.org_session, "bind"):
            return True
        result = await self.org_session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": (f"crm_csv_import:{operation}:{self.organisation_id}:{import_id}")},
        )
        return bool(result.scalar())

    async def _load_ready_batch(self, import_id: UUID) -> list[CRMImportRow]:
        """Load one stable row-number-ordered worker batch."""
        if not hasattr(self.org_session, "bind"):
            return [
                row
                for row in await self._load_rows(import_id)
                if row.status in {CRMImportRowStatus.PREVIEW, CRMImportRowStatus.READY}
            ][:IMPORT_BATCH_SIZE]
        stmt = (
            select(CRMImportRow)
            .where(
                col(CRMImportRow.organisation_id) == self.organisation_id,
                col(CRMImportRow.import_job_id) == import_id,
                col(CRMImportRow.status).in_(
                    [CRMImportRowStatus.PREVIEW.value, CRMImportRowStatus.READY.value]
                ),
            )
            .order_by(col(CRMImportRow.row_number).asc())
            .limit(IMPORT_BATCH_SIZE)
        )
        result = await self.org_session.execute(stmt)
        return list(result.scalars().all())

    async def _has_ready_rows(self, import_id: UUID) -> bool:
        """Check for a continuation after the current batch has been flushed."""
        if not hasattr(self.org_session, "bind"):
            return any(
                row.status in {CRMImportRowStatus.PREVIEW, CRMImportRowStatus.READY}
                for row in await self._load_rows(import_id)
            )
        stmt = (
            select(col(CRMImportRow.row_id))
            .where(
                col(CRMImportRow.organisation_id) == self.organisation_id,
                col(CRMImportRow.import_job_id) == import_id,
                col(CRMImportRow.status).in_(
                    [CRMImportRowStatus.PREVIEW.value, CRMImportRowStatus.READY.value]
                ),
            )
            .limit(1)
        )
        result = await self.org_session.execute(stmt)
        return result.first() is not None

    @asynccontextmanager
    async def _row_savepoint(self):
        """Rollback only the current row while retaining earlier batch outcomes."""
        begin_nested = getattr(self.org_session, "begin_nested", None)
        if begin_nested is None:
            yield
            return
        async with begin_nested():
            yield

    async def _prepare_row(self, row: CRMImportRow, metadata: dict[str, Any]) -> dict[str, Any]:
        """Parse and resolve a row completely before any CRM record is written."""
        mapping = metadata.get("mapping") or {}
        normalized_values = {
            target: str(row.raw_values.get(header) or "").strip()
            for header, target in mapping.items()
        }
        if not normalized_values.get("deal.name"):
            raise CRMImportServiceError("Deal name is required", 400)
        if not any(normalized_values.get(field) for field in ACCOUNT_IDENTITY_FIELDS):
            raise CRMImportServiceError("Usable Account identity is required", 400)
        if not any(normalized_values.get(field) for field in CONTACT_IDENTITY_FIELDS):
            raise CRMImportServiceError("Usable Contact identity is required", 400)
        account_linkedin = normalized_values.get("account.linkedin_url")
        contact_linkedin = normalized_values.get("contact.linkedin_url")
        contact_email = normalized_values.get("contact.email")
        if account_linkedin and not is_account_linkedin_url(account_linkedin):
            raise CRMImportServiceError(ACCOUNT_LINKEDIN_URL_ERROR, 400)
        if contact_linkedin and (
            len(contact_linkedin) > 500 or not is_person_linkedin_url(contact_linkedin)
        ):
            raise CRMImportServiceError(PERSON_LINKEDIN_URL_ERROR, 400)
        if contact_email and (len(contact_email) > 320 or not is_valid_email(contact_email)):
            raise CRMImportServiceError("Invalid Contact email", 400)

        pipeline_id = _metadata_uuid(metadata, "pipeline_id")
        default_stage_id = _metadata_uuid(metadata, "default_stage_id")
        if pipeline_id is None or default_stage_id is None:
            raise CRMImportServiceError("Pipeline and default stage are required", 400)
        stage_id = await self._resolve_stage(
            pipeline_id,
            normalized_values.get("deal.stage"),
            default_stage_id,
        )
        owner_user_id = await self._resolve_owner(
            normalized_values.get("deal.owner"),
            _metadata_uuid(metadata, "default_owner_user_id"),
        )
        resolved_stage = next(
            stage for stage in self._stage_cache[pipeline_id] if stage.stage_id == stage_id
        )
        normalized_values["resolved.stage_name"] = resolved_stage.name
        if owner_user_id:
            resolved_owner = next(
                user for user in await self._load_org_users() if user.user_id == owner_user_id
            )
            normalized_values["resolved.owner_name"] = (
                f"{resolved_owner.first_name} {resolved_owner.last_name}".strip()
            )

        column_formats = metadata.get("column_formats") or {}
        target_types = self._target_type_cache.get(pipeline_id)
        if target_types is None:
            catalog = await self.get_targets(pipeline_id)
            target_types = {item["target_field"]: item["value_type"] for item in catalog.targets}
            self._target_type_cache[pipeline_id] = target_types
        custom_properties: dict[str, Any] = {}
        for header, target in mapping.items():
            if not target.startswith("deal.custom."):
                continue
            value: Any = row.raw_values.get(header)
            if str(value or "").strip():
                value = _parse_import_custom_value(
                    value,
                    value_type=target_types.get(target, "text"),
                    date_order=column_formats.get(header),
                    field_key=target.removeprefix("deal.custom."),
                )
            custom_properties[target.removeprefix("deal.custom.")] = value
        custom_writer = DealCustomFieldWriteService(self.org_session, self.organisation_id)
        custom_properties = await custom_writer.validate_custom_properties(custom_properties)
        await custom_writer.validate_required_on_create(custom_properties)

        lock_keys = _identity_lock_keys(self.organisation_id, normalized_values)
        return {
            "normalized_values": normalized_values,
            "stage_id": stage_id,
            "owner_user_id": owner_user_id,
            "custom_properties": custom_properties,
            "lock_keys": lock_keys,
        }

    async def _resolve_stage(
        self, pipeline_id: UUID, raw_value: str | None, default_stage_id: UUID
    ) -> UUID:
        """Resolve UUID/name/key only inside the selected pipeline."""
        stages = self._stage_cache.get(pipeline_id)
        if stages is None:
            _pipeline, stages = await self._load_import_pipeline(pipeline_id)
            self._stage_cache[pipeline_id] = stages
        value = (raw_value or "").strip()
        if not value:
            if default_stage_id in {stage.stage_id for stage in stages}:
                return default_stage_id
            raise CRMImportServiceError(
                "Default stage is no longer active in the selected pipeline", 400
            )
        try:
            candidate_id = UUID(value)
        except ValueError:
            candidate_id = None
        if candidate_id:
            if candidate_id in {stage.stage_id for stage in stages}:
                return candidate_id
            raise CRMImportServiceError("Unknown stage UUID for selected pipeline", 400)
        normalized = _normalize_match_value(value)
        matches = [
            stage
            for stage in stages
            if normalized
            in {
                _normalize_match_value(stage.name),
                _normalize_match_value(stage.label or ""),
                _normalize_match_value(stage.stage_key or ""),
            }
        ]
        if len(matches) == 1:
            return UUID(str(matches[0].stage_id))
        if not matches:
            raise CRMImportServiceError(f"Unknown stage: {value}", 400)
        raise CRMImportServiceError(f"Ambiguous stage: {value}", 400)

    async def _resolve_owner(
        self, raw_value: str | None, default_owner_user_id: UUID | None
    ) -> UUID | None:
        """Resolve an organisation app user, falling back only when configured."""
        value = (raw_value or "").strip()
        if not value:
            return default_owner_user_id
        users = await self._load_org_users()
        try:
            candidate_id = UUID(value)
        except ValueError:
            candidate_id = None
        if candidate_id:
            matches = [user for user in users if user.user_id == candidate_id]
        elif "@" in value:
            normalized_email = normalize_email(value)
            matches = [
                user for user in users if normalize_email(str(user.email)) == normalized_email
            ]
        else:
            normalized_name = _normalize_match_value(value)
            matches = [
                user
                for user in users
                if _normalize_match_value(f"{user.first_name} {user.last_name}") == normalized_name
            ]
        if len(matches) == 1:
            return UUID(str(matches[0].user_id))
        if default_owner_user_id is not None:
            return default_owner_user_id
        reason = "ambiguous" if len(matches) > 1 else "not found"
        raise CRMImportServiceError(f"Deal owner {reason}: {value}", 400)

    async def _load_org_users(self) -> list[User]:
        """Load organisation users once per batch for owner resolution and snapshots."""
        if self.master_session is None:
            raise CRMImportServiceError("Master session is required to resolve owner", 500)
        if self._org_users_cache is None:
            stmt = select(User).where(col(User.organisation_id) == self.organisation_id)
            result = await self.master_session.execute(stmt)
            self._org_users_cache = list(result.scalars().all())
        return self._org_users_cache

    async def _acquire_identity_locks(self, prepared: dict[str, Any]) -> None:
        """Lock normalized identities in stable order to prevent cross-import duplicates."""
        for lock_key in prepared["lock_keys"]:
            await self.org_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            )

    async def _load_import_pipeline(
        self, pipeline_id: UUID
    ) -> tuple[CRMPipeline, list[CRMDealStage]]:
        """Load an active internal pipeline and its active mapping stages."""
        pipeline = await self.org_session.get(CRMPipeline, pipeline_id)
        integration = await InternalCRMSetupService(
            self.org_session,
            self.organisation_id,
            self.user_id or AUTOMATED_PIPELINE_USER_ID,
        ).get_internal_integration()
        if (
            pipeline is None
            or pipeline.organisation_id != self.organisation_id
            or pipeline.is_deleted
            or not pipeline.is_active
            or integration is None
            or pipeline.integration_id != integration.integration_id
        ):
            raise CRMImportServiceError("Pipeline not found or not writable", 404)
        stmt = (
            select(CRMDealStage)
            .where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.pipeline_id) == pipeline_id,
                col(CRMDealStage.integration_id) == integration.integration_id,
                col(CRMDealStage.is_deleted).is_(False),
                col(CRMDealStage.is_active).is_(True),
            )
            .order_by(col(CRMDealStage.display_order).asc(), col(CRMDealStage.name).asc())
        )
        result = await self.org_session.execute(stmt)
        stages = list(result.scalars().all())
        if not has_required_pipeline_shape(stages):
            raise CRMImportServiceError(required_pipeline_shape_message(), 409)
        return pipeline, stages

    async def _validate_configuration(
        self,
        *,
        pipeline_id: UUID,
        default_stage_id: UUID,
        default_owner_user_id: UUID | None,
    ) -> None:
        """Validate pipeline, stage, and owner at the API trust boundary."""
        _pipeline, stages = await self._load_import_pipeline(pipeline_id)
        if default_stage_id not in {stage.stage_id for stage in stages}:
            raise CRMImportServiceError("Default stage is not active in the selected pipeline", 400)
        if default_owner_user_id is None:
            return
        if self.master_session is None:
            raise CRMImportServiceError("Master session is required to validate deal owner", 500)
        # The organisation predicate is the owner trust boundary; a user UUID
        # alone must never make another tenant's member assignable to this import.
        stmt = select(User).where(
            col(User.user_id) == default_owner_user_id,
            col(User.organisation_id) == self.organisation_id,
        )
        result = await self.master_session.execute(stmt)
        if result.scalars().first() is None:
            raise CRMImportServiceError("Default owner is not an organisation user", 400)

    async def _validate_mapping_draft(
        self,
        *,
        metadata: dict[str, Any],
        mapping: dict[str, str],
        ignored_columns: list[str],
        column_formats: dict[str, str],
        pipeline_id: UUID,
    ) -> None:
        """Authoritatively validate one complete mapping draft against fresh schema."""
        headers = list((metadata.get("analysis") or {}).get("headers") or [])
        header_set = set(headers)
        mapped_headers = set(mapping)
        ignored_set = set(ignored_columns)
        duplicate_ignored = sorted(
            header for header, count in Counter(ignored_columns).items() if count > 1
        )
        if duplicate_ignored:
            raise CRMImportServiceError(
                "Duplicate ignored CSV columns", 400, {"columns": duplicate_ignored}
            )
        unknown_headers = sorted((mapped_headers | ignored_set) - header_set)
        overlap = sorted(mapped_headers & ignored_set)
        unresolved = sorted(header_set - mapped_headers - ignored_set)
        if unknown_headers or overlap or unresolved:
            raise CRMImportServiceError(
                "CSV mapping must resolve or explicitly ignore every column",
                400,
                {
                    "unknown_columns": unknown_headers,
                    "conflicting_columns": overlap,
                    "unresolved_columns": unresolved,
                },
            )

        catalog = await self.get_targets(pipeline_id)
        supported = {item["target_field"] for item in catalog.targets}
        unsupported = sorted(set(mapping.values()) - supported)
        if unsupported:
            raise CRMImportServiceError(
                "Unsupported or read-only CRM import target field",
                400,
                {"unsupported_fields": unsupported},
            )
        targets = set(mapping.values())
        missing: list[str] = []
        if "deal.name" not in targets:
            missing.append("deal.name")
        if not targets.intersection(ACCOUNT_IDENTITY_FIELDS):
            missing.append("account identity")
        if not targets.intersection(CONTACT_IDENTITY_FIELDS):
            missing.append("contact identity")
        required_custom = {
            item["target_field"]
            for item in catalog.targets
            if item["required"] and item["custom_field_id"] is not None
        }
        missing.extend(sorted(required_custom - targets))
        if missing:
            raise CRMImportServiceError(
                "Required CRM import mappings are missing", 400, {"missing_fields": missing}
            )

        invalid_format_headers = sorted(set(column_formats) - mapped_headers)
        invalid_formats = sorted(
            header
            for header, value in column_formats.items()
            if value not in {"DMY", "MDY"}
            or not _is_date_target(mapping.get(header, ""), catalog.targets)
        )
        if invalid_format_headers or invalid_formats:
            raise CRMImportServiceError(
                "Invalid CSV date format configuration",
                400,
                {"invalid_columns": sorted(set(invalid_format_headers + invalid_formats))},
            )
        analysis = metadata.get("analysis") or {}
        ambiguous_in_file = set(analysis.get("ambiguous_numeric_date_headers") or [])
        sample_rows = list(analysis.get("sample_rows") or [])
        ambiguous_date_headers = sorted(
            header
            for header, target in mapping.items()
            if _is_date_target(target, catalog.targets)
            and header not in column_formats
            and (
                header in ambiguous_in_file
                or any(
                    _is_ambiguous_numeric_date(str(row.get(header) or "")) for row in sample_rows
                )
            )
        )
        if ambiguous_date_headers:
            raise CRMImportServiceError(
                "Ambiguous numeric dates require DMY or MDY",
                400,
                {"columns": ambiguous_date_headers},
            )

    async def _ensure_mapping_can_be_saved(self, job: DealJobs, metadata: dict[str, Any]) -> None:
        can_retry = (
            metadata.get("crm_import_status") == "completed"
            and int((job.result_data or {}).get("failed_rows") or 0) > 0
        )
        if metadata.get("crm_import_status") != "ready_for_mapping" and not can_retry:
            raise CRMImportServiceError(
                "Import analysis must complete before mapping can be saved", 409
            )
        if not metadata.get("analysis"):
            raise CRMImportServiceError("Import analysis is missing", 409)
        if not await self._load_rows(job.job_id):
            raise CRMImportServiceError("Import has no analyzed rows", 409)

    async def _prepare_failed_rows_for_retry(self, job: DealJobs, metadata: dict[str, Any]) -> None:
        """Reset failed rows only; successful Deals remain immutable across retries."""
        rows = await self._load_rows(job.job_id)
        failed_rows = [row for row in rows if row.status == CRMImportRowStatus.FAILED]
        if not failed_rows:
            raise CRMImportServiceError("Import has no failed rows to retry", 409)
        for row in failed_rows:
            attempt_count = int(_row_audit(row).get("attempt_count") or 0)
            row.status = CRMImportRowStatus.READY
            row.normalized_values = {_ROW_AUDIT_KEY: {"attempt_count": attempt_count}}
            row.account_id = None
            row.contact_id = None
            row.deal_id = None
            row.error_code = None
            row.error_message = None
            self.org_session.add(row)
        metadata["crm_import_status"] = "ready_for_mapping"
        job.status = JobStatus.PENDING
        job.result_data = _counts_from_rows(rows)

    async def _ensure_import_can_start(self, job: DealJobs, metadata: dict[str, Any]) -> None:
        if metadata.get("crm_import_status") != "ready_for_mapping":
            raise CRMImportServiceError("Import is not ready to start", 409)
        if not metadata.get("analysis"):
            raise CRMImportServiceError("Import analysis is missing", 409)
        if not metadata.get("mapping"):
            raise CRMImportServiceError("Import mapping has not been saved", 409)
        if not await self._load_rows(job.job_id):
            raise CRMImportServiceError("Import has no analyzed rows", 409)

    async def _send_process_task(self, import_id: UUID) -> None:
        if self.task_sender is None:
            return
        result = self.task_sender(organisation_id=self.organisation_id, import_job_id=import_id)
        if inspect.isawaitable(result):
            await result

    async def _commit(self) -> None:
        await self.org_session.flush()
        await self.org_session.commit()


def _values_for_prefix(
    raw_values: dict[str, Any],
    mapping: dict[str, str],
    prefix: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for header, target in mapping.items():
        if not target.startswith(prefix):
            continue
        value = raw_values.get(header)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            values[target.removeprefix(prefix)] = text
    return values


def _deal_payload_from_values(
    values: dict[str, str],
    *,
    column_formats: dict[str, str],
    mapping: dict[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name, value in values.items():
        if field_name.startswith("custom.") or field_name in {"owner", "stage"}:
            continue
        if field_name == "amount":
            try:
                amount = _parse_import_number(value)
            except (TypeError, ValueError) as exc:
                raise CRMImportServiceError("Invalid Deal amount", 400) from exc
            if amount < 0 or amount > 9_999_999_999_999_999.99:
                raise CRMImportServiceError("Invalid Deal amount", 400)
            payload[field_name] = amount
        elif field_name == "probability":
            probability_text = value.removesuffix("%").strip()
            try:
                probability = _parse_import_number(probability_text)
            except (TypeError, ValueError) as exc:
                raise CRMImportServiceError("Invalid Deal probability", 400) from exc
            if probability < 0 or probability > 100:
                raise CRMImportServiceError("Deal probability must be between 0 and 100", 400)
            payload[field_name] = probability
        elif field_name == "currency":
            currency = value.strip().upper()
            if len(currency) != 3 or not currency.isalpha():
                raise CRMImportServiceError("Deal currency must be a 3-letter code", 400)
            payload[field_name] = currency
        elif field_name in {"expected_close_date", "source_created_at"}:
            header = next(
                (header for header, target in mapping.items() if target == f"deal.{field_name}"),
                "",
            )
            payload[field_name] = _parse_import_datetime(
                value, date_order=column_formats.get(header)
            )
        else:
            payload[field_name] = value
    return payload


def _parse_import_datetime(value: str, *, date_order: str | None = None) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        formats = ["%d-%b-%Y", "%d %b %Y", "%b %d %Y", "%B %d %Y"]
        if date_order == "DMY":
            formats.insert(0, "%d/%m/%Y")
            formats.insert(1, "%d-%m-%Y")
            formats.insert(2, "%d/%m/%y")
            formats.insert(3, "%d-%m-%y")
        elif date_order == "MDY":
            formats.insert(0, "%m/%d/%Y")
            formats.insert(1, "%m-%d-%Y")
            formats.insert(2, "%m/%d/%y")
            formats.insert(3, "%m-%d-%y")
        for date_format in formats:
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        else:
            raise CRMImportServiceError(f"Invalid date value: {value}", 400)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_import_number(value: Any) -> float:
    """Parse bounded CSV number syntax without changing shared CRM coercion."""
    if isinstance(value, bool):
        raise ValueError("Invalid number")
    if isinstance(value, (int, float)):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Invalid number")
        return parsed
    text_value = str(value).strip()
    negative = text_value.startswith("(") and text_value.endswith(")")
    if negative:
        text_value = text_value[1:-1].strip()
    elif text_value.startswith("(") or text_value.endswith(")"):
        raise ValueError("Invalid number")
    if sum(unicodedata.category(character) == "Sc" for character in text_value) > 1:
        raise ValueError("Invalid number")
    text_value = "".join(
        character for character in text_value if unicodedata.category(character) != "Sc"
    )
    if len(re.findall(r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])", text_value)) > 1:
        raise ValueError("Invalid number")
    text_value = re.sub(r"^[A-Za-z]{3}\s+", "", text_value)
    text_value = re.sub(r"\s+[A-Za-z]{3}$", "", text_value).strip()
    if not re.fullmatch(
        r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)",
        text_value,
    ):
        raise ValueError("Invalid number")
    parsed = float(text_value.replace(",", ""))
    parsed = -abs(parsed) if negative else parsed
    if not math.isfinite(parsed):
        raise ValueError("Invalid number")
    return parsed


def _parse_import_custom_value(
    value: Any,
    *,
    value_type: str,
    date_order: str | None,
    field_key: str,
) -> Any:
    """Convert CSV-only syntax before canonical custom-field validation."""
    try:
        if value_type in {"number", "currency"}:
            return _parse_import_number(value)
        if value_type == "percentage":
            text_value = str(value).strip()
            if text_value.endswith("%"):
                text_value = text_value[:-1].strip()
            if "%" in text_value:
                raise ValueError("Invalid percentage")
            return _parse_import_number(text_value)
        if value_type in {"date", "datetime"}:
            parsed = _parse_import_datetime(str(value), date_order=date_order)
            return parsed.date().isoformat() if value_type == "date" else parsed
        if value_type == "multiselect":
            text_value = str(value).strip()
            if text_value.startswith("["):
                parsed = json.loads(text_value)
                if not isinstance(parsed, list):
                    raise ValueError("Multiselect JSON must be an array")
                return parsed
            return [item.strip() for item in text_value.split(";") if item.strip()]
    except (TypeError, ValueError, RecursionError) as exc:
        raise CRMImportServiceError(f"Invalid value for custom field '{field_key}'", 400) from exc
    return value


def _metadata_uuid(metadata: dict[str, Any], key: str) -> UUID | None:
    value = metadata.get(key)
    if value in {None, ""}:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise CRMImportServiceError(f"Invalid {key}", 400) from exc


def _normalize_match_value(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).split())


def _escape_like(value: str) -> str:
    """Treat user search text literally inside a SQL LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _identity_lock_keys(organisation_id: UUID, normalized_values: dict[str, str]) -> list[str]:
    identities = {
        "account:domain": normalize_account_domain(
            normalized_values.get("account.domain") or normalized_values.get("account.website")
        ),
        "account:linkedin": normalize_linkedin_url(normalized_values.get("account.linkedin_url")),
        "account:name": normalize_company_name(normalized_values.get("account.name")),
        "contact:email": normalize_email(normalized_values.get("contact.email")),
        "contact:linkedin": normalize_linkedin_url(normalized_values.get("contact.linkedin_url")),
    }
    return sorted(
        f"crm_csv_identity:{organisation_id}:{identity_type}:{value}"
        for identity_type, value in identities.items()
        if value
    )


def _is_date_target(target: str, catalog_targets: list[dict[str, Any]]) -> bool:
    return any(
        item["target_field"] == target and item["value_type"] in {"date", "datetime"}
        for item in catalog_targets
    )


def _is_ambiguous_numeric_date(value: str) -> bool:
    match = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})\s*", value)
    return bool(match and int(match.group(1)) <= 12 and int(match.group(2)) <= 12)


def _row_error_code(exc: Exception) -> str:
    message = str(getattr(exc, "message", exc)).lower()
    if "stage" in message:
        return "invalid_stage"
    if "owner" in message:
        return "invalid_owner"
    if "conflict" in message:
        return "conflicting_identity"
    if "ambiguous" in message:
        return "ambiguous_identity"
    return "validation_error"


def _row_error_field(exc: Exception) -> str | None:
    message = str(getattr(exc, "message", exc)).lower()
    custom_field = re.search(r"custom field '([^']+)'", message)
    if custom_field:
        return f"deal.custom.{custom_field.group(1)}"
    for token, field in (
        ("deal name", "deal.name"),
        ("contact email", "contact.email"),
        ("account", "account.identity"),
        ("contact", "contact.identity"),
        ("stage", "deal.stage"),
        ("owner", "deal.owner"),
        ("probability", "deal.probability"),
        ("amount", "deal.amount"),
        ("currency", "deal.currency"),
        ("forecast_category", "deal.forecast_category"),
        ("deal_type", "deal.deal_type"),
        ("priority", "deal.priority"),
        ("lead_source", "deal.lead_source"),
        ("date", "deal.date"),
        ("custom field", "deal.custom"),
    ):
        if token in message:
            return field
    return None


def _row_audit(row: CRMImportRow) -> dict[str, Any]:
    """Read supplementary audit state without adding database columns."""
    audit = (row.normalized_values or {}).get(_ROW_AUDIT_KEY)
    return dict(audit) if isinstance(audit, dict) else {}


def _update_row_audit(row: CRMImportRow, **changes: Any) -> None:
    """Persist import-only audit fields inside the ledger's existing JSONB payload."""
    normalized = dict(row.normalized_values or {})
    audit = _row_audit(row)
    audit.update(
        {key: str(value) if isinstance(value, UUID) else value for key, value in changes.items()}
    )
    normalized[_ROW_AUDIT_KEY] = audit
    row.normalized_values = normalized


def _counts_from_rows(rows: list[CRMImportRow], *, total_rows: int | None = None) -> dict[str, int]:
    successful = [row for row in rows if row.status == CRMImportRowStatus.CREATED]
    failed = [row for row in rows if row.status == CRMImportRowStatus.FAILED]
    skipped = [row for row in rows if row.status == CRMImportRowStatus.SKIPPED_DUPLICATE]
    return {
        "total_rows": total_rows if total_rows is not None else len(rows),
        "processed_rows": len(successful) + len(failed) + len(skipped),
        "successful_rows": len(successful),
        "created_deals": len(successful),
        "created_accounts": sum(
            _row_audit(row).get("account_outcome") == "created" for row in successful
        ),
        "matched_accounts": sum(
            _row_audit(row).get("account_outcome") == "matched" for row in successful
        ),
        "created_contacts": sum(
            _row_audit(row).get("contact_outcome") == "created" for row in successful
        ),
        "matched_contacts": sum(
            _row_audit(row).get("contact_outcome") == "matched" for row in successful
        ),
        "failed_rows": len(failed),
        "skipped_rows": len(skipped),
    }


def _increment_counts(
    existing: dict[str, Any],
    committed_rows: list[CRMImportRow],
    *,
    total_rows: int,
) -> dict[str, int]:
    """Add one newly committed batch to the durable parent counters."""
    delta = _counts_from_rows(committed_rows, total_rows=0)
    return {
        "total_rows": total_rows,
        **{
            key: int(existing.get(key) or 0) + value
            for key, value in delta.items()
            if key != "total_rows"
        },
    }


def _row_target_type(
    account_id: UUID | None,
    contact_id: UUID | None,
    deal_id: UUID | None,
) -> str | None:
    if deal_id:
        return "deal"
    if contact_id:
        return "contact"
    if account_id:
        return "account"
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)
