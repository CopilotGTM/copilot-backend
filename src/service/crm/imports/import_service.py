from __future__ import annotations

import csv
import inspect
import io
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
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
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.imports.csv_mapping import suggest_csv_mappings
from src.service.crm.imports.csv_parser import (
    MAX_CSV_FILE_SIZE_BYTES,
    parse_crm_csv_content,
    sanitize_csv_cell_for_export,
)

SUPPORTED_IMPORT_FIELDS = {
    "account.name",
    "account.domain",
    "account.website",
    "account.industry",
    "account.phone",
    "account.city",
    "account.state",
    "account.country",
    "account.address",
    "account.postal_code",
    "account.linkedin_url",
    "contact.name",
    "contact.first_name",
    "contact.last_name",
    "contact.email",
    "contact.job_title",
    "contact.phone",
    "contact.mobile_phone",
    "contact.website",
    "contact.address",
    "contact.city",
    "contact.state",
    "contact.country",
    "contact.postal_code",
    "contact.linkedin_url",
    "deal.name",
    "deal.amount",
    "deal.currency",
    "deal.source_created_at",
    "deal.expected_close_date",
    "deal.description",
    "deal.deal_type",
    "deal.priority",
    "deal.lead_source",
    "deal.probability",
    "deal.forecast_category",
}


@dataclass(frozen=True)
class CRMImportPresignResult:
    import_id: UUID
    upload_url: str
    storage_path: str


@dataclass(frozen=True)
class CRMImportJobResult:
    job: DealJobs


@dataclass(frozen=True)
class CRMImportRowsResult:
    items: list[CRMImportRow]
    next_cursor: int | None
    has_more: bool


class CRMImportServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class CRMImportService:
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

    async def presign(
        self,
        original_filename: str,
        *,
        mode: str = "account_contact",
        options: dict[str, Any] | None = None,
    ) -> CRMImportPresignResult:
        if self.user_id is None:
            raise CRMImportServiceError("User id is required to create an import", 400)
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
            "crm_import_status": "uploading",
            "presigned_at": _now_iso(),
        }
        self.org_session.add(job)
        upload_url = await self.storage.generate_write_sas(storage_path)
        await self._commit()
        return CRMImportPresignResult(job.job_id, upload_url, storage_path)

    async def finalize(self, import_id: UUID) -> CRMImportJobResult:
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
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
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        storage_path = metadata.get("storage_path")
        if not storage_path:
            raise CRMImportServiceError("Import is missing its storage path", 400)

        content = await self.storage.get_blob_content(storage_path)
        parsed = parse_crm_csv_content(content, sample_size=25_000)
        if parsed.blocking_errors:
            job.status = JobStatus.FAILED
            job.error_message = parsed.blocking_errors[0].message
            metadata["crm_import_status"] = "failed"
            metadata["parse_errors"] = [asdict(error) for error in parsed.blocking_errors]
            job.job_metadata = metadata
            await self._commit()
            return CRMImportJobResult(job)

        suggestions = suggest_csv_mappings(parsed.headers, parsed.sample_rows)
        metadata.update(
            {
                "crm_import_status": "ready_for_mapping",
                "analysis": {
                    "headers": parsed.headers,
                    "total_rows": parsed.total_rows,
                    "sampled_rows": len(parsed.sample_rows),
                },
                "mapping_suggestions": [asdict(suggestion) for suggestion in suggestions],
                "analyzed_at": _now_iso(),
            }
        )
        job.job_metadata = metadata

        existing_rows = await self._load_rows(import_id)
        existing_numbers = {row.row_number for row in existing_rows}
        for row_number, raw_values in enumerate(parsed.sample_rows, start=1):
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
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def save_mapping(
        self,
        import_id: UUID,
        mapping: dict[str, str],
        *,
        options: dict[str, Any] | None = None,
    ) -> CRMImportJobResult:
        target_fields = list(mapping.values())
        unsupported = sorted(set(target_fields) - SUPPORTED_IMPORT_FIELDS)
        if unsupported:
            raise CRMImportServiceError(
                "Unsupported CRM import target field",
                400,
                {"unsupported_fields": unsupported},
            )
        duplicate_targets = sorted(
            target for target, count in Counter(target_fields).items() if count > 1
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
        metadata["mapping"] = mapping
        metadata["options"] = options or {}
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def start(self, import_id: UUID) -> CRMImportJobResult:
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        if job.status == JobStatus.IN_PROGRESS and metadata.get("crm_import_status") == "importing":
            await self._send_process_task(job.job_id)
            return CRMImportJobResult(job)
        await self._ensure_import_can_start(job, metadata)

        job.status = JobStatus.IN_PROGRESS
        metadata["crm_import_status"] = "importing"
        metadata["started_at"] = _now_iso()
        job.job_metadata = metadata
        self.org_session.add(job)
        await self._commit()
        await self._send_process_task(job.job_id)
        return CRMImportJobResult(job)

    async def process_next_batch(self, import_id: UUID) -> CRMImportJobResult:
        job = await self._load_job(import_id)
        self._current_actor_user_id = job.user_id
        metadata = dict(job.job_metadata or {})
        mapping = metadata.get("mapping") or {}
        if not mapping:
            raise CRMImportServiceError("Import mapping has not been saved", 409)

        rows = [
            row
            for row in await self._load_rows(import_id)
            if row.status in {CRMImportRowStatus.PREVIEW, CRMImportRowStatus.READY}
        ]
        if not rows and job.result_data:
            return CRMImportJobResult(job)
        counts = {
            "total_rows": len(rows),
            "processed_rows": 0,
            "created_accounts": 0,
            "created_contacts": 0,
            "created_deals": 0,
            "skipped_duplicates": 0,
            "failed_rows": 0,
        }
        account_writer = AccountWriteService(self.org_session, self.organisation_id)
        contact_writer = ContactWriteService(self.org_session, self.organisation_id)

        for row in rows:
            counts["processed_rows"] += 1
            try:
                account_id, account_created = await self._process_account(
                    row, mapping, account_writer
                )
                contact_id, contact_created = await self._process_contact(
                    row,
                    mapping,
                    contact_writer,
                    account_id=account_id,
                )
                deal_id, deal_created = await self._process_deal(
                    row,
                    mapping,
                    account_id=account_id,
                    contact_id=contact_id,
                )
                row.account_id = account_id
                row.contact_id = contact_id
                row.deal_id = deal_id
                row.target_type = _row_target_type(account_id, contact_id, deal_id)
                row.status = CRMImportRowStatus.CREATED
                row.error_code = None
                row.error_message = None
                counts["created_accounts"] += int(account_created)
                counts["created_contacts"] += int(contact_created)
                counts["created_deals"] += int(deal_created)
            except (
                AccountWriteServiceError,
                ContactWriteServiceError,
                DealWriteServiceError,
            ) as exc:
                if exc.status_code == 409:
                    row.status = CRMImportRowStatus.SKIPPED_DUPLICATE
                    row.error_code = "duplicate"
                    counts["skipped_duplicates"] += 1
                else:
                    row.status = CRMImportRowStatus.FAILED
                    row.error_code = "validation_error"
                    counts["failed_rows"] += 1
                row.error_message = exc.message
            except Exception as exc:
                row.status = CRMImportRowStatus.FAILED
                row.error_code = "unexpected_error"
                row.error_message = exc.__class__.__name__
                counts["failed_rows"] += 1
            self.org_session.add(row)

        job.status = JobStatus.COMPLETED if counts["failed_rows"] == 0 else JobStatus.FAILED
        metadata["crm_import_status"] = (
            "completed" if job.status == JobStatus.COMPLETED else "failed"
        )
        metadata["completed_at"] = _now_iso()
        job.job_metadata = metadata
        job.result_data = counts
        self.org_session.add(job)
        await self._commit()
        return CRMImportJobResult(job)

    async def get_status(self, import_id: UUID) -> CRMImportJobResult:
        return CRMImportJobResult(await self._load_job(import_id))

    async def list_rows(
        self,
        import_id: UUID,
        *,
        cursor: int | None = None,
        limit: int = 100,
        status: str | None = None,
    ) -> CRMImportRowsResult:
        await self._load_job(import_id)
        rows = sorted(await self._load_rows(import_id), key=lambda row: row.row_number)
        if status:
            rows = [row for row in rows if _enum_value(row.status) == status]
        if cursor is not None:
            rows = [row for row in rows if row.row_number > cursor]

        # copilotgtm: V1 import size is capped at 25k rows, so in-memory row
        # paging is acceptable; move this to SQL cursor queries if the cap grows.
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = page[-1].row_number if page else None
        return CRMImportRowsResult(items=page, next_cursor=next_cursor, has_more=has_more)

    async def build_error_csv(self, import_id: UUID) -> str:
        await self._load_job(import_id)
        rows = sorted(await self._load_rows(import_id), key=lambda row: row.row_number)
        error_rows = [row for row in rows if row.error_code or row.error_message]
        headers = []
        seen_headers: set[str] = set()
        for row in error_rows:
            for header in row.raw_values:
                if header not in seen_headers:
                    headers.append(header)
                    seen_headers.add(header)

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["row_number", "status", "error_code", "error_message", *headers])
        for row in error_rows:
            writer.writerow(
                [
                    sanitize_csv_cell_for_export(row.row_number),
                    sanitize_csv_cell_for_export(_enum_value(row.status)),
                    sanitize_csv_cell_for_export(row.error_code),
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
    ) -> tuple[UUID | None, bool]:
        values = _values_for_prefix(row.raw_values, mapping, "account.")
        if not any(values.values()):
            return None, False
        try:
            result = await account_writer.create_account(
                AccountCreateCommand(
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
                ),
                context=AccountWriteContext(
                    source="crm_import",
                    actor_user_id=self._current_actor_user_id,
                    created_via="crm_import",
                ),
            )
        except AccountWriteServiceError as exc:
            if exc.status_code != 409 or not exc.data or not exc.data.get("company_id"):
                raise
            account = await account_writer.load_org_company(UUID(exc.data["company_id"]))
            return account.company_id, False
        return result.account.company_id, result.created

    async def _process_contact(
        self,
        row: CRMImportRow,
        mapping: dict[str, str],
        contact_writer: ContactWriteService,
        *,
        account_id: UUID | None,
    ) -> tuple[UUID | None, bool]:
        values = _values_for_prefix(row.raw_values, mapping, "contact.")
        if not any(values.values()):
            return None, False
        name = values.get("name") or " ".join(
            value for value in [values.get("first_name"), values.get("last_name")] if value
        )
        result = await contact_writer.create_contact(
            ContactCreateCommand(
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
            ),
            context=ContactWriteContext(
                source="crm_import",
                actor_user_id=self._current_actor_user_id,
            ),
        )
        return result.contact.contact_id, result.created

    async def _process_deal(
        self,
        row: CRMImportRow,
        mapping: dict[str, str],
        *,
        account_id: UUID | None,
        contact_id: UUID | None,
    ) -> tuple[UUID | None, bool]:
        values = _values_for_prefix(row.raw_values, mapping, "deal.")
        if not any(values.values()):
            return None, False
        if self.master_session is None:
            raise DealWriteServiceError(
                "Master session is required to import deals", status_code=500
            )
        if self._current_actor_user_id is None:
            raise DealWriteServiceError("User id is required to import deals", status_code=400)

        payload = _deal_payload_from_values(values)
        account_values = _values_for_prefix(row.raw_values, mapping, "account.")
        if account_id:
            payload["account"] = {"company_id": account_id}
        elif any(account_values.values()):
            payload["account"] = account_values

        contact_values = _values_for_prefix(row.raw_values, mapping, "contact.")
        primary_contact_name = contact_values.get("name") or " ".join(
            value
            for value in [contact_values.get("first_name"), contact_values.get("last_name")]
            if value
        )
        if primary_contact_name:
            payload["primary_contact_name"] = primary_contact_name
        if contact_values.get("email"):
            payload["primary_contact_email"] = contact_values["email"]
        if contact_id:
            payload["contact_participants"] = [{"contact_id": contact_id}]

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

    async def _ensure_mapping_can_be_saved(self, job: DealJobs, metadata: dict[str, Any]) -> None:
        if metadata.get("crm_import_status") != "ready_for_mapping":
            raise CRMImportServiceError(
                "Import analysis must complete before mapping can be saved", 409
            )
        if not metadata.get("analysis"):
            raise CRMImportServiceError("Import analysis is missing", 409)
        if not await self._load_rows(job.job_id):
            raise CRMImportServiceError("Import has no analyzed rows", 409)

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


def _deal_payload_from_values(values: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name, value in values.items():
        if field_name in {"amount", "probability"}:
            payload[field_name] = float(value)
        elif field_name == "currency":
            payload[field_name] = value.upper()
        elif field_name == "expected_close_date":
            payload[field_name] = _parse_import_datetime(value)
        else:
            payload[field_name] = value
    return payload


def _parse_import_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


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
