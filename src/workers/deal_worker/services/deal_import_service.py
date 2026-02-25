"""Service for processing bulk deal imports."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables.user_models import User as MasterUser
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMPipeline,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_notes_service import DealNotesService
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)

_EMAIL_SPLIT_RE = re.compile(r"[;,]+")
_AMOUNT_CLEAN_RE = re.compile(r"[,$]")
_DOMAIN_CLEAN_RE = re.compile(r"^https?://", re.IGNORECASE)
_COMPANY_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CONTACT_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CUSTOM_PREFIX_RE = re.compile(r"^custom_(.+)$")
_VALID_DEAL_STATUSES = {s.value for s in CRMDealStatus}
_CUSTOM_KEY_ALIASES = {
    # Backward compatibility for client-specific historical sheets.
    "deepmatrix_revenue_inr": "our_revenue_inr",
}
_VALID_IMPORT_MODES = {"create_only", "update_only", "upsert"}
_CUSTOM_TRUE_VALUES = {"true", "yes", "y", "1"}
_CUSTOM_FALSE_VALUES = {"false", "no", "n", "0"}

# Accept both canonical internal keys and template-facing deal_* keys.
# All incoming CSV headers are normalized to lowercase and remapped here.
_CSV_HEADER_ALIASES = {
    "deal_name": "deal_name",
    "name": "deal_name",
    "deal_amount": "amount",
    "amount": "amount",
    "deal_currency": "currency",
    "currency": "currency",
    "deal_status": "deal_status",
    "status": "deal_status",
    "deal_probability": "probability",
    "probability": "probability",
    "deal_close_date": "close_date",
    "close_date": "close_date",
    "deal_expected_close_date": "expected_close_date",
    "expected_close_date": "expected_close_date",
    "deal_closed_won_at": "closed_won_at",
    "closed_won_at": "closed_won_at",
    "deal_closed_lost_at": "closed_lost_at",
    "closed_lost_at": "closed_lost_at",
    "deal_closed_reason": "closed_reason",
    "closed_reason": "closed_reason",
    "deal_crm_url": "crm_url",
    "crm_url": "crm_url",
    "deal_stage_name": "stage_name",
    "stage_name": "stage_name",
    "deal_forecast_category": "forecast_category",
    "forecast_category": "forecast_category",
    "deal_type": "deal_type",
    "deal_priority": "priority",
    "priority": "priority",
    "deal_lead_source": "lead_source",
    "lead_source": "lead_source",
    "deal_owner_email": "owner_email",
    "owner_email": "owner_email",
    "company_name": "company_name",
    "company_domain": "company_domain",
    "deal_primary_contact_name": "primary_contact_name",
    "primary_contact_name": "primary_contact_name",
    "deal_primary_contact_email": "primary_contact_email",
    "primary_contact_email": "primary_contact_email",
    "deal_source_created_at": "source_created_at",
    "source_created_at": "source_created_at",
    "deal_last_activity_at": "last_activity_at",
    "last_activity_at": "last_activity_at",
    "deal_description": "description",
    "description": "description",
    "company_website": "company_website",
    "company_linkedin": "company_linkedin",
    "company_industry": "company_industry",
    "company_country": "company_country",
    "deal_note": "import_note",
    "import_note": "import_note",
    "deal_external_ref": "deal_external_ref",
    "import_source_ref": "deal_external_ref",
    "deal_team_member_emails": "team_member_emails",
    "team_member_emails": "team_member_emails",
}


@dataclass(slots=True)
class DealImportContext:
    integration_id: UUID
    default_pipeline_id: UUID
    pipeline: CRMPipeline
    stage_by_name: dict[str, CRMDealStage]
    custom_fields_by_key: dict[str, CustomFieldRuntime]


@dataclass(slots=True)
class CustomFieldRuntime:
    data_type: str
    validation: dict


@dataclass(slots=True)
class ActorInfo:
    user_id: UUID
    user_name: str
    user_email: str


class DealImportProcessingService:
    """Orchestrates CSV import for internal CRM deals."""

    def __init__(
        self,
        organisation_id: UUID,
        actor_user_id: UUID,
        storage_path: str,
        publisher: DealJobRedisPublisher,
        import_mode: str = "upsert",
        emit_row_events: bool = True,
        strict_validation: bool = False,
        pipeline_id: UUID | None = None,
    ) -> None:
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.storage_path = storage_path
        self.publisher = publisher
        self.import_mode = self._normalize_import_mode(import_mode)
        self.emit_row_events = emit_row_events
        self.strict_validation = strict_validation
        self.pipeline_id = pipeline_id

    async def process(self) -> dict:
        self.publisher.publish_progress("Downloading import file", "download", 0.05)
        content = await self._download_blob()

        self.publisher.publish_progress("Parsing import file", "parse", 0.15)
        rows = self._parse_csv(content)
        if not rows:
            raise DealWriteServiceError("Import file contains no rows", status_code=400)

        context = await self._load_context()
        # Batch-resolve all emails upfront so we hit the DB once, not once per row
        user_map = await self._resolve_user_map(rows)
        actor_info = await self._resolve_actor_info()

        return await self._process_rows(rows, context, user_map, actor_info)

    async def _download_blob(self) -> bytes:
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            return await storage.get_blob_content(self.storage_path)
        finally:
            await storage.close()

    def _parse_csv(self, content: bytes) -> list[dict[str, str | None]]:
        # utf-8-sig strips the BOM that Excel adds when saving as CSV
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise DealWriteServiceError("Import file is missing a header row", status_code=400)

        rows: list[dict[str, str | None]] = []
        for row_number, row in enumerate(reader, start=2):
            extras = row.get(None)
            if extras:
                raise DealWriteServiceError(
                    f"Invalid CSV format on row {row_number}: too many columns",
                    status_code=400,
                )

            normalized: dict[str, str | None] = {}
            for raw_key, raw_value in row.items():
                if raw_key is None:
                    continue
                key = raw_key.strip().lower()
                if not key:
                    continue
                canonical_key = _CSV_HEADER_ALIASES.get(key, key)
                cleaned = self._clean_value(raw_value)
                # If multiple aliases map to the same canonical key, keep first non-empty value.
                if canonical_key in normalized and normalized[canonical_key]:
                    continue
                normalized[canonical_key] = cleaned

            if any(normalized.values()):
                rows.append(normalized)
        return rows

    async def _load_context(self) -> DealImportContext:
        """Load pipeline stages and custom field definitions for the import.

        When self.pipeline_id is set (from job metadata), loads that specific
        pipeline's stages. Otherwise falls back to the default pipeline.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            setup_service = InternalCRMSetupService(
                session, self.organisation_id, self.actor_user_id
            )
            result = await setup_service.get_status()
            if not result:
                raise DealWriteServiceError("Internal CRM is not configured", status_code=409)

            integration_id = result.integration.integration_id

            if self.pipeline_id:
                # Load stages for the explicitly-specified pipeline
                pipeline_stmt = select(CRMPipeline).where(
                    col(CRMPipeline.pipeline_id) == self.pipeline_id,
                    col(CRMPipeline.organisation_id) == self.organisation_id,
                    col(CRMPipeline.is_deleted).is_(False),
                )
                pipeline_result = await session.execute(pipeline_stmt)
                target_pipeline = pipeline_result.scalars().first()
                if not target_pipeline:
                    raise DealWriteServiceError(
                        f"Pipeline {self.pipeline_id} not found", status_code=404
                    )
                target_pipeline_id = target_pipeline.pipeline_id

                stage_stmt = select(CRMDealStage).where(
                    col(CRMDealStage.pipeline_id) == target_pipeline_id,
                    col(CRMDealStage.organisation_id) == self.organisation_id,
                    col(CRMDealStage.is_deleted).is_(False),
                )
                stage_result = await session.execute(stage_stmt)
                pipeline_stages = list(stage_result.scalars().all())
            else:
                target_pipeline_id = result.pipeline.pipeline_id
                pipeline_stages = result.stages

            # Case-insensitive lookup so CSV values like "Closed Won" and "closed won" both match
            stage_by_name: dict[str, CRMDealStage] = {}
            for stage in pipeline_stages:
                if stage.name:
                    stage_by_name[stage.name.lower()] = stage
                if stage.label:
                    stage_by_name[stage.label.lower()] = stage

            custom_field_stmt = select(CRMCustomFieldDefinition).where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
                col(CRMCustomFieldDefinition.is_active).is_(True),
            )
            custom_field_result = await session.execute(custom_field_stmt)
            custom_fields_by_key = {
                row.key: CustomFieldRuntime(
                    data_type=row.data_type.value
                    if isinstance(row.data_type, CRMCustomFieldDataType)
                    else str(row.data_type),
                    validation=(row.validation or {}),
                )
                for row in custom_field_result.scalars().all()
            }

            # Resolve the pipeline object for context
            if self.pipeline_id and target_pipeline:
                context_pipeline = target_pipeline
            else:
                context_pipeline = result.pipeline

            return DealImportContext(
                integration_id=integration_id,
                default_pipeline_id=target_pipeline_id,
                pipeline=context_pipeline,
                stage_by_name=stage_by_name,
                custom_fields_by_key=custom_fields_by_key,
            )

        raise DealWriteServiceError("Failed to load import context", status_code=500)

    async def _resolve_user_map(self, rows: Iterable[dict[str, str | None]]) -> dict[str, UUID]:
        emails = set()
        for row in rows:
            owner_email = self._clean_value(row.get("owner_email"))
            if owner_email:
                emails.add(owner_email.lower())
            for email in self._split_emails(row.get("team_member_emails")):
                emails.add(email.lower())

        if not emails:
            return {}

        async for master_session in acquire_master_async_session():
            stmt = (
                select(col(MasterUser.email), col(MasterUser.user_id))
                .where(col(MasterUser.organisation_id) == self.organisation_id)
                .where(func.lower(col(MasterUser.email)).in_(list(emails)))
            )
            result = await master_session.execute(stmt)
            return {email.lower(): user_id for email, user_id in result.all()}

        return {}

    async def _resolve_actor_info(self) -> ActorInfo:
        """Look up the importing user's name and email for note attribution."""
        async for master_session in acquire_master_async_session():
            stmt = select(
                col(MasterUser.first_name),
                col(MasterUser.last_name),
                col(MasterUser.email),
            ).where(col(MasterUser.user_id) == self.actor_user_id)
            result = await master_session.execute(stmt)
            row = result.one_or_none()
            if row:
                first_name, last_name, email = row
                return ActorInfo(
                    user_id=self.actor_user_id,
                    user_name=f"{first_name} {last_name}".strip(),
                    user_email=str(email),
                )

        # Fallback — should not happen in practice
        return ActorInfo(
            user_id=self.actor_user_id,
            user_name="Import User",
            user_email="",
        )

    async def _create_import_note(
        self,
        *,
        deal: CRMDeal,
        description: str,
        org_session: AsyncSession,
        actor_info: ActorInfo,
    ) -> None:
        """Create a deal note from the CSV description column.

        Uses DealNotesService.create_user_note() which deduplicates on
        body + subject, so re-importing the same file won't create duplicates.
        Does NOT trigger the deal insight pipeline (too expensive for bulk).
        """
        notes_service = DealNotesService(
            org_session=org_session,
            organisation_id=self.organisation_id,
        )
        await notes_service.create_user_note(
            deal_id=deal.deal_id,
            content=description,
            subject="Import Note",
            user_id=actor_info.user_id,
            user_name=actor_info.user_name,
            user_email=actor_info.user_email,
            created_via="import",
        )

    async def _process_rows(
        self,
        rows: list[dict[str, str | None]],
        context: DealImportContext,
        user_map: dict[str, UUID],
        actor_info: ActorInfo,
    ) -> dict:
        total_rows = len(rows)
        created: list[dict] = []
        updated: list[dict] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        warnings: list[dict] = []
        session_opened = False

        async for org_session in acquire_org_async_session(self.organisation_id):
            async for master_session in acquire_master_async_session():
                session_opened = True
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=self.organisation_id,
                    actor_user_id=self.actor_user_id,
                )

                for idx, row in enumerate(rows, start=1):
                    progress = 0.15 + (0.85 * (idx / total_rows))
                    if idx == 1 or idx == total_rows or idx % 10 == 0:
                        self.publisher.publish_progress(
                            f"Processing row {idx} of {total_rows}",
                            "create",
                            progress,
                        )

                    row_number = idx + 1  # account for header row
                    input_deal_name = self._resolve_input_deal_name(row)
                    if self.emit_row_events:
                        self.publisher.publish_deal_started(idx, row_number, input_deal_name)
                        self.publisher.publish_deal_progress(
                            idx,
                            row_number,
                            "validate",
                            "Validating and normalizing deal row",
                            input_deal_name,
                        )
                    try:
                        payload, row_warnings = self._build_payload(row, context, user_map)
                        row_warnings.extend(
                            await self._resolve_account_warnings(
                                org_session=org_session,
                                context=context,
                                payload=payload,
                            )
                        )
                        row_warnings.extend(
                            await self._resolve_contact_warnings(
                                org_session=org_session,
                                context=context,
                                payload=payload,
                            )
                        )
                        deal_name = (
                            (payload.get("name") or input_deal_name) if payload else input_deal_name
                        )
                        row_warnings_with_number = [
                            {**warning, "row_number": row_number} for warning in row_warnings
                        ]
                        warnings.extend(row_warnings_with_number)

                        if self.emit_row_events:
                            self.publisher.publish_deal_progress(
                                idx,
                                row_number,
                                "upsert_deal",
                                "Resolving create/update action",
                                deal_name,
                            )

                        existing_deal = await self._find_matching_deal(
                            org_session=org_session,
                            row=row,
                            payload=payload,
                            context=context,
                        )
                        row_action = self._select_import_action(existing_deal)

                        if row_action == "skip":
                            skipped_entry = {
                                "row_number": row_number,
                                "deal_id": str(existing_deal.deal_id) if existing_deal else None,
                                "deal_name": deal_name,
                                "reason": "match_exists",
                            }
                            skipped.append(skipped_entry)
                            if self.emit_row_events:
                                self.publisher.publish_deal_completed(
                                    deal_index=idx,
                                    row_number=row_number,
                                    outcome="skipped",
                                    deal_id=str(existing_deal.deal_id) if existing_deal else None,
                                    deal_name=deal_name,
                                    warnings=row_warnings_with_number,
                                )
                        elif row_action == "update":
                            if self.emit_row_events:
                                self.publisher.publish_deal_progress(
                                    idx,
                                    row_number,
                                    "upsert_deal",
                                    "Updating existing deal record",
                                    deal_name,
                                )
                            if existing_deal is None:
                                raise DealWriteServiceError(
                                    "No matching deal found for update",
                                    status_code=404,
                                )
                            deal = await self._update_deal(
                                existing_deal=existing_deal,
                                payload=payload,
                                context=context,
                                service=service,
                                org_session=org_session,
                                actor_info=actor_info,
                            )
                            updated.append(
                                {
                                    "row_number": row_number,
                                    "deal_id": str(deal.deal_id),
                                    "deal_name": deal.name,
                                }
                            )
                            if self.emit_row_events:
                                self.publisher.publish_deal_progress(
                                    idx,
                                    row_number,
                                    "commit",
                                    "Deal update committed",
                                    deal.name,
                                )
                                self.publisher.publish_deal_completed(
                                    deal_index=idx,
                                    row_number=row_number,
                                    outcome="updated",
                                    deal_id=str(deal.deal_id),
                                    deal_name=deal.name,
                                    warnings=row_warnings_with_number,
                                )
                        else:
                            if self.emit_row_events:
                                self.publisher.publish_deal_progress(
                                    idx,
                                    row_number,
                                    "upsert_deal",
                                    "Creating deal record",
                                    deal_name,
                                )
                            deal = await self._create_deal(
                                payload, service, org_session, actor_info
                            )
                            created.append(
                                {
                                    "row_number": row_number,
                                    "deal_id": str(deal.deal_id),
                                    "deal_name": deal.name,
                                }
                            )
                            if self.emit_row_events:
                                self.publisher.publish_deal_progress(
                                    idx,
                                    row_number,
                                    "commit",
                                    "Deal create committed",
                                    deal.name,
                                )
                                self.publisher.publish_deal_completed(
                                    deal_index=idx,
                                    row_number=row_number,
                                    outcome="created",
                                    deal_id=str(deal.deal_id),
                                    deal_name=deal.name,
                                    warnings=row_warnings_with_number,
                                )
                    except DealWriteServiceError as exc:
                        error_code = self._error_code_for_write_error(exc)
                        failed.append(
                            {
                                "row_number": row_number,
                                "deal_name": self._clean_value(row.get("deal_name")),
                                "error": exc.message,
                            }
                        )
                        if self.emit_row_events:
                            self.publisher.publish_deal_failed(
                                deal_index=idx,
                                row_number=row_number,
                                deal_name=input_deal_name,
                                error_code=error_code,
                                message=exc.message,
                            )
                    except Exception as exc:
                        message = str(exc)
                        failed.append(
                            {
                                "row_number": row_number,
                                "deal_name": self._clean_value(row.get("deal_name")),
                                "error": message,
                            }
                        )
                        if self.emit_row_events:
                            self.publisher.publish_deal_failed(
                                deal_index=idx,
                                row_number=row_number,
                                deal_name=input_deal_name,
                                error_code="unexpected_error",
                                message=message,
                            )
                    finally:
                        if self.emit_row_events:
                            self.publisher.publish_summary_tick(
                                processed=idx,
                                total=total_rows,
                                created=len(created),
                                updated=len(updated),
                                skipped=len(skipped),
                                failed=len(failed),
                                warnings=len(warnings),
                            )
                # break out of nested async-for loops — sessions released on exit
                break
            break

        if not session_opened:
            raise DealWriteServiceError("Failed to open database session", status_code=500)

        return {
            "total_rows": total_rows,
            "created_count": len(created),
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "warning_count": len(warnings),
            "import_mode": self.import_mode,
            "emit_row_events": self.emit_row_events,
            "strict_validation": self.strict_validation,
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "warnings": warnings,
        }

    @staticmethod
    def _normalize_import_mode(import_mode: str | None) -> str:
        mode = (import_mode or "upsert").strip().lower()
        if mode not in _VALID_IMPORT_MODES:
            raise DealWriteServiceError(
                f"Invalid import_mode: {import_mode}. Expected one of: {sorted(_VALID_IMPORT_MODES)}",
                status_code=400,
            )
        return mode

    def _select_import_action(self, existing_deal: CRMDeal | None) -> str:
        if self.import_mode == "create_only":
            return "skip" if existing_deal else "create"
        if self.import_mode == "update_only":
            if existing_deal is None:
                raise DealWriteServiceError("No matching deal found for update", status_code=404)
            return "update"
        return "update" if existing_deal else "create"

    async def _find_matching_deal(
        self,
        *,
        org_session: AsyncSession,
        row: Mapping[str, str | None],
        payload: dict,
        context: DealImportContext,
    ) -> CRMDeal | None:
        """Find an existing deal by deal_name + company_name (account name).

        Match → update the deal.  No match → create a new deal.
        Searches across ALL pipelines.
        """
        deal_name = self._clean_value(payload.get("name"))
        if not deal_name:
            return None

        account_payload = payload.get("account") or {}
        company_name = self._clean_value(account_payload.get("name")) or self._clean_value(
            row.get("company_name")
        )

        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == context.integration_id,
            col(CRMDeal.is_deleted).is_(False),
            func.lower(col(CRMDeal.name)) == deal_name.lower(),
        )

        if company_name:
            stmt = stmt.where(func.lower(col(CRMDeal.company_name)) == company_name.lower())

        result = await org_session.execute(stmt)
        match = result.scalars().first()

        if match:
            logger.debug(
                "Matched existing deal %s (name=%s, company=%s)",
                match.deal_id,
                deal_name,
                company_name,
            )
        else:
            logger.debug(
                "No matching deal found (name=%s, company=%s) — will create",
                deal_name,
                company_name,
            )
        return match

    async def _resolve_account_warnings(
        self,
        *,
        org_session: AsyncSession,
        context: DealImportContext,
        payload: dict,
    ) -> list[dict]:
        account_payload = payload.get("account")
        if not account_payload:
            return []

        company_domain = self._normalize_domain(account_payload.get("domain"))
        company_name = self._clean_value(account_payload.get("name"))
        if company_domain or not company_name:
            return []

        normalized_name = self._normalize_company_name(company_name)
        if not normalized_name:
            return []

        # Domain is missing, so name collisions need human review/merge later.
        stmt = select(col(CRMCompany.company_id)).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == context.integration_id,
            col(CRMCompany.is_deleted).is_(False),
            func.lower(func.trim(col(CRMCompany.name))) == normalized_name,
        )
        result = await org_session.execute(stmt)
        existing_count = len(list(result.scalars().all()))
        if existing_count <= 1:
            return []

        return [
            {
                "warning": "ambiguous_account_name",
                "company_name": company_name,
                "existing_matches": existing_count,
            }
        ]

    async def _resolve_contact_warnings(
        self,
        *,
        org_session: AsyncSession,
        context: DealImportContext,
        payload: dict,
    ) -> list[dict]:
        contact_email = self._normalize_email(payload.get("primary_contact_email"))
        contact_name = self._clean_value(payload.get("primary_contact_name"))
        if not contact_name and not contact_email:
            return []
        if contact_email:
            return []

        warnings: list[dict] = [
            {
                "warning": "contact_email_missing",
                "contact_name": contact_name,
            }
        ]
        normalized_name = self._normalize_contact_name(contact_name)
        if not normalized_name:
            return warnings

        stmt = select(col(CRMContact.contact_id)).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.integration_id) == context.integration_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.full_name))) == normalized_name,
        )
        result = await org_session.execute(stmt)
        existing_count = len(list(result.scalars().all()))
        if existing_count > 1:
            warnings.append(
                {
                    "warning": "ambiguous_contact_name",
                    "contact_name": contact_name,
                    "existing_matches": existing_count,
                }
            )
        return warnings

    def _resolve_input_deal_name(self, row: Mapping[str, str | None]) -> str | None:
        """Build a stable deal label for SSE frames even before row validation succeeds."""
        return (
            self._clean_value(row.get("deal_name"))
            or self._clean_value(row.get("company_name"))
            or self._clean_value(row.get("company_domain"))
        )

    @staticmethod
    def _error_code_for_write_error(exc: DealWriteServiceError) -> str:
        """Map import write errors into stable machine-readable codes for the frontend."""
        message = (exc.message or "").lower()
        if "owner not found" in message:
            return "owner_not_found"
        if "stage not found" in message:
            return "stage_not_found"
        if "ambiguous match" in message:
            return "ambiguous_match"
        if "ambiguous contact" in message:
            return "ambiguous_contact_name"
        if "no matching deal found" in message:
            return "deal_not_found"
        if "missing deal_name" in message:
            return "missing_required"
        if "invalid " in message:
            return "invalid_value"
        if "internal crm is not configured" in message:
            return "integration_not_configured"
        return "deal_write_error"

    def _build_payload(
        self,
        row: Mapping[str, str | None],
        context: DealImportContext,
        user_map: dict[str, UUID],
    ) -> tuple[dict, list[dict]]:
        warnings: list[dict] = []

        # Fallback chain: deal_name → company_name → company_domain
        deal_name = self._clean_value(row.get("deal_name"))
        company_name = self._clean_value(row.get("company_name"))
        company_domain = self._clean_value(row.get("company_domain"))

        if not deal_name:
            deal_name = company_name or company_domain
        if not deal_name:
            raise DealWriteServiceError(
                "Missing deal_name (or company_name/company_domain fallback)", status_code=400
            )

        stage_name = self._clean_value(row.get("stage_name"))
        stage = None
        if stage_name:
            stage = context.stage_by_name.get(stage_name.lower())
            if not stage:
                raise DealWriteServiceError(f"Stage not found: {stage_name}", status_code=404)

        # Owner not found = row fails when explicitly provided. If omitted, we default to importer.
        owner_email = self._clean_value(row.get("owner_email"))
        owner_user_id = None
        owner_email_provided = bool(owner_email)
        if owner_email:
            owner_user_id = user_map.get(owner_email.lower())
            if not owner_user_id:
                raise DealWriteServiceError(f"Owner not found: {owner_email}", status_code=404)
        else:
            owner_user_id = self.actor_user_id
            warnings.append({"warning": "owner_defaulted_to_importer"})

        # Team member not found = warning only, so one bad email doesn't block the row
        team_emails = self._split_emails(row.get("team_member_emails"))
        team_members: list[dict] = []
        for email in team_emails:
            user_id = user_map.get(email.lower())
            if not user_id:
                warnings.append({"email": email, "warning": "team_member_not_found"})
                continue
            team_members.append({"user_id": user_id, "role": "member"})

        amount_raw = self._clean_value(row.get("amount"))
        try:
            amount = self._parse_amount(amount_raw)
        except ValueError as exc:
            raise DealWriteServiceError(
                f"Invalid amount: {row.get('amount')}",
                status_code=400,
            ) from exc
        currency = self._clean_value(row.get("currency"))
        if currency:
            currency = currency.upper()

        close_date_raw = self._clean_value(row.get("close_date"))
        close_date = self._parse_date(close_date_raw)
        if close_date_raw and close_date is None:
            raise DealWriteServiceError(
                f"Invalid close_date: {row.get('close_date')}",
                status_code=400,
            )

        expected_close_raw = self._clean_value(row.get("expected_close_date"))
        expected_close_date = self._parse_date(expected_close_raw)
        if expected_close_raw and expected_close_date is None:
            raise DealWriteServiceError(
                f"Invalid expected_close_date: {row.get('expected_close_date')}",
                status_code=400,
            )

        source_created_raw = self._clean_value(row.get("source_created_at"))
        source_created_at = self._parse_date(source_created_raw)
        if source_created_raw and source_created_at is None:
            raise DealWriteServiceError(
                f"Invalid source_created_at: {row.get('source_created_at')}",
                status_code=400,
            )

        last_activity_raw = self._clean_value(row.get("last_activity_at"))
        last_activity_at = self._parse_date(last_activity_raw)
        if last_activity_raw and last_activity_at is None:
            raise DealWriteServiceError(
                f"Invalid last_activity_at: {row.get('last_activity_at')}",
                status_code=400,
            )

        closed_won_raw = self._clean_value(row.get("closed_won_at"))
        closed_won_at = self._parse_date(closed_won_raw)
        if closed_won_raw and closed_won_at is None:
            raise DealWriteServiceError(
                f"Invalid closed_won_at: {row.get('closed_won_at')}",
                status_code=400,
            )

        closed_lost_raw = self._clean_value(row.get("closed_lost_at"))
        closed_lost_at = self._parse_date(closed_lost_raw)
        if closed_lost_raw and closed_lost_at is None:
            raise DealWriteServiceError(
                f"Invalid closed_lost_at: {row.get('closed_lost_at')}",
                status_code=400,
            )

        # Parse deal_status enum
        deal_status_raw = self._clean_value(row.get("deal_status"))
        if deal_status_raw:
            deal_status_lower = deal_status_raw.lower()
            if deal_status_lower not in _VALID_DEAL_STATUSES:
                raise DealWriteServiceError(
                    f"Invalid deal_status: {deal_status_raw} "
                    f"(must be one of: {', '.join(sorted(_VALID_DEAL_STATUSES))})",
                    status_code=400,
                )
            deal_status = deal_status_lower
        else:
            deal_status = None

        # Parse probability (0-100)
        probability_raw = self._clean_value(row.get("probability"))
        probability = None
        if probability_raw:
            try:
                probability = float(probability_raw)
            except ValueError as exc:
                raise DealWriteServiceError(
                    f"Invalid probability: {probability_raw} (must be a number 0-100)",
                    status_code=400,
                ) from exc
            if probability < 0 or probability > 100:
                raise DealWriteServiceError(
                    f"Invalid probability: {probability_raw} (must be between 0 and 100)",
                    status_code=400,
                )

        # Custom fields are cast using org-level definitions captured at import start.
        custom_properties, custom_warnings, has_custom_columns = self._extract_custom_properties(
            row=row,
            custom_fields_by_key=context.custom_fields_by_key,
        )
        warnings.extend(custom_warnings)
        deal_external_ref = self._clean_value(row.get("deal_external_ref"))
        if deal_external_ref:
            custom_properties["deal_external_ref"] = deal_external_ref

        account_payload = {
            "domain": company_domain,
            "name": company_name,
            "website": self._clean_value(row.get("company_website")),
            "linkedin_url": self._clean_value(row.get("company_linkedin")),
            "industry": self._clean_value(row.get("company_industry")),
            "country": self._clean_value(row.get("company_country"))
            or self._clean_value(row.get("geography")),
        }
        if not any(account_payload.values()):
            account_payload = None

        payload = {
            "name": deal_name,
            "amount": amount,
            "currency": currency,
            "deal_status": deal_status,
            "probability": probability,
            "forecast_category": self._clean_value(row.get("forecast_category")),
            "close_date": close_date,
            "expected_close_date": expected_close_date,
            "source_created_at": source_created_at,
            "last_activity_at": last_activity_at,
            "closed_won_at": closed_won_at,
            "closed_lost_at": closed_lost_at,
            "closed_reason": self._clean_value(row.get("closed_reason")),
            "deal_type": self._clean_value(row.get("deal_type")),
            "priority": self._clean_value(row.get("priority")),
            "lead_source": self._clean_value(row.get("lead_source")),
            "crm_url": self._clean_value(row.get("crm_url")),
            "description": self._clean_value(row.get("description")),
            "primary_contact_name": self._clean_value(row.get("primary_contact_name")),
            "primary_contact_email": self._clean_value(row.get("primary_contact_email")),
            "primary_contact_columns_mapped": (
                "primary_contact_name" in row or "primary_contact_email" in row
            ),
            "owner_user_id": owner_user_id,
            "owner_email_provided": owner_email_provided,
            "pipeline_id": context.default_pipeline_id,
            "stage_id": stage.stage_id if stage else None,
            "account": account_payload,
            "team_members": team_members or None,
            "custom_properties": custom_properties if has_custom_columns else None,
            "import_note": self._clean_value(row.get("import_note")),
            "created_via": "import",
        }
        return payload, warnings

    async def _create_deal(
        self,
        payload: dict,
        service: DealWriteService,
        org_session: AsyncSession,
        actor_info: ActorInfo,
    ):
        # Each row commits independently so one failure doesn't roll back prior rows
        try:
            deal = await service.create_deal(payload)
            import_note = payload.get("import_note")
            if import_note:
                await self._create_import_note(
                    deal=deal,
                    description=import_note,
                    org_session=org_session,
                    actor_info=actor_info,
                )
            await org_session.commit()
            await org_session.refresh(deal)
            return deal
        except Exception:
            await org_session.rollback()
            raise

    async def _update_deal(
        self,
        *,
        existing_deal: CRMDeal,
        payload: dict,
        context: DealImportContext,
        service: DealWriteService,
        org_session: AsyncSession,
        actor_info: ActorInfo,
    ):
        # Re-apply the full mapped row for matched deals. Explicit empty cells
        # are passed as None so existing values are cleared on update.
        update_payload = {
            "name": payload.get("name"),
            "amount": payload.get("amount"),
            "currency": payload.get("currency"),
            "deal_status": payload.get("deal_status"),
            "probability": payload.get("probability"),
            "forecast_category": payload.get("forecast_category"),
            "close_date": payload.get("close_date"),
            "expected_close_date": payload.get("expected_close_date"),
            "closed_won_at": payload.get("closed_won_at"),
            "closed_lost_at": payload.get("closed_lost_at"),
            "closed_reason": payload.get("closed_reason"),
            "crm_url": payload.get("crm_url"),
            "source_created_at": payload.get("source_created_at"),
            "last_activity_at": payload.get("last_activity_at"),
            "description": payload.get("description"),
            "deal_type": payload.get("deal_type"),
            "priority": payload.get("priority"),
            "lead_source": payload.get("lead_source"),
            "custom_properties": payload.get("custom_properties"),
        }

        try:
            deal = existing_deal
            if update_payload:
                # Import extraction already validated custom properties against
                # cached field definitions — skip the per-row DB round-trip.
                deal = await service.update_deal(
                    existing_deal.deal_id,
                    update_payload,
                    skip_custom_validation=True,
                )

            # Ownership behavior:
            # - explicit owner_email in CSV -> assign resolved owner
            # - owner_email omitted -> preserve existing owner, but backfill importer when empty
            owner_user_id = payload.get("owner_user_id")
            should_assign_owner = bool(payload.get("owner_email_provided"))
            if not should_assign_owner and deal.owner_id is None and owner_user_id is not None:
                should_assign_owner = True

            if should_assign_owner and owner_user_id != deal.owner_id:
                deal = await service.assign_owner(existing_deal.deal_id, owner_user_id)

            # Pipeline + stage handling: if the import targets a different pipeline,
            # reassign the deal to the new pipeline and clean up old stage history.
            target_pipeline_id = payload.get("pipeline_id")
            stage_id = payload.get("stage_id")
            pipeline_changed = (
                target_pipeline_id and deal.pipeline_id and target_pipeline_id != deal.pipeline_id
            )

            if pipeline_changed:
                await self._reassign_pipeline(
                    deal=deal,
                    pipeline=context.pipeline,
                    stage_id=stage_id,
                    org_session=org_session,
                )
            elif stage_id and deal.stage_id != stage_id:
                deal = await service.move_stage(existing_deal.deal_id, stage_id)

            account_payload = payload.get("account")
            if account_payload and any(account_payload.values()):
                deal = await service.update_account(existing_deal.deal_id, account_payload)

            contact_name = payload.get("primary_contact_name")
            contact_email = payload.get("primary_contact_email")
            contact_columns_mapped = payload.get("primary_contact_columns_mapped", False)
            if contact_name or contact_email:
                deal = await service.update_primary_contact(
                    existing_deal.deal_id,
                    primary_contact_name=contact_name,
                    primary_contact_email=contact_email,
                    company_name=(account_payload or {}).get("name")
                    if isinstance(account_payload, dict)
                    else None,
                )
            elif contact_columns_mapped:
                # Columns mapped but empty — clear existing primary contact fields.
                deal.primary_contact_name = None
                deal.primary_contact_email = None

            import_note = payload.get("import_note")
            if import_note:
                await self._create_import_note(
                    deal=deal,
                    description=import_note,
                    org_session=org_session,
                    actor_info=actor_info,
                )

            await org_session.commit()
            await org_session.refresh(deal)
            return deal
        except Exception:
            await org_session.rollback()
            raise

    async def _reassign_pipeline(
        self,
        *,
        deal: CRMDeal,
        pipeline: CRMPipeline,
        stage_id: UUID | None,
        org_session: AsyncSession,
    ) -> None:
        """Move a deal to a different pipeline: update pipeline fields, delete
        old stage history, and set the new stage."""
        now = datetime.now(UTC)

        # Delete old stage history — stages from the previous pipeline are irrelevant
        old_history_stmt = select(CRMDealStageHistory).where(
            col(CRMDealStageHistory.deal_id) == deal.deal_id,
        )
        old_history_result = await org_session.execute(old_history_stmt)
        for record in old_history_result.scalars().all():
            await org_session.delete(record)

        # Update pipeline fields on the deal
        deal.pipeline_id = pipeline.pipeline_id
        deal.pipeline_external_id = pipeline.external_id
        deal.pipeline_name = pipeline.name

        # Resolve and apply the new stage
        if stage_id:
            stage = await org_session.get(CRMDealStage, stage_id)
        else:
            # Fall back to the first stage in the new pipeline
            first_stage_stmt = (
                select(CRMDealStage)
                .where(
                    col(CRMDealStage.pipeline_id) == pipeline.pipeline_id,
                    col(CRMDealStage.organisation_id) == self.organisation_id,
                    col(CRMDealStage.is_deleted).is_(False),
                )
                .order_by(col(CRMDealStage.display_order).asc())
                .limit(1)
            )
            stage_result = await org_session.execute(first_stage_stmt)
            stage = stage_result.scalars().first()

        if stage:
            deal.stage_id = stage.stage_id
            deal.stage_external_id = stage.external_id
            deal.stage_name = stage.name

            # Record the first history entry in the new pipeline
            history_id = uuid4()
            history = CRMDealStageHistory(
                history_id=history_id,
                organisation_id=self.organisation_id,
                integration_id=deal.integration_id,
                external_id=str(history_id),
                deal_id=deal.deal_id,
                stage_id=stage.stage_id,
                stage_external_id=stage.external_id,
                entered_at=now,
                synced_at=now,
                last_seen_at=now,
            )
            org_session.add(history)

        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        org_session.add(deal)

        logger.info(
            "Reassigned deal %s to pipeline %s (stage=%s)",
            deal.deal_id,
            pipeline.name,
            stage.name if stage else "none",
        )

    @staticmethod
    def _clean_value(value: object | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            cleaned_parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
            cleaned = ",".join(cleaned_parts)
            return cleaned or None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _parse_amount(value: str | None) -> float | None:
        if not value:
            return None
        cleaned = _AMOUNT_CLEAN_RE.sub("", value).strip()
        if not cleaned:
            return None
        return float(cleaned)

    @staticmethod
    def _normalize_domain(value: object | None) -> str | None:
        cleaned = DealImportProcessingService._clean_value(value)
        if not cleaned:
            return None
        cleaned = _DOMAIN_CLEAN_RE.sub("", cleaned)
        cleaned = cleaned.split("/")[0]
        cleaned = cleaned.lower()
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        return cleaned or None

    @staticmethod
    def _normalize_company_name(value: object | None) -> str | None:
        cleaned = DealImportProcessingService._clean_value(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        cleaned = _COMPANY_NAME_NON_ALNUM_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    @staticmethod
    def _normalize_contact_name(value: object | None) -> str | None:
        cleaned = DealImportProcessingService._clean_value(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        cleaned = _CONTACT_NAME_NON_ALNUM_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    @staticmethod
    def _normalize_email(value: object | None) -> str | None:
        cleaned = DealImportProcessingService._clean_value(value)
        if not cleaned:
            return None
        return cleaned.lower()

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            from dateutil import parser as date_parser  # type: ignore[import-untyped]

            parsed = date_parser.parse(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except Exception:
            return None

    @staticmethod
    def _split_emails(value: object | None) -> list[str]:
        raw = DealImportProcessingService._clean_value(value)
        if not raw:
            return []
        parts = [p.strip() for p in _EMAIL_SPLIT_RE.split(raw)]
        return [p for p in parts if p]

    def _extract_custom_properties(
        self,
        *,
        row: Mapping[str, str | None],
        custom_fields_by_key: dict[str, CustomFieldRuntime],
    ) -> tuple[dict[str, object | None], list[dict[str, str]], bool]:
        props: dict[str, object | None] = {}
        warnings: list[dict[str, str]] = []
        has_custom_columns = False
        for col_name, raw_value in row.items():
            if not col_name:
                continue
            match = _CUSTOM_PREFIX_RE.match(str(col_name).strip())
            if not match:
                continue
            has_custom_columns = True
            key = match.group(1)
            key = _CUSTOM_KEY_ALIASES.get(key, key)
            definition = custom_fields_by_key.get(key)

            raw_clean_value = DealImportProcessingService._clean_value(raw_value)
            if raw_clean_value is None:
                if definition:
                    # Explicitly clear known custom fields when CSV cell is empty.
                    props[key] = None
                continue
            if not definition:
                props[key] = raw_clean_value
                continue

            try:
                parsed_value = self._coerce_custom_value(
                    key=key,
                    raw_value=raw_clean_value,
                    definition=definition,
                )
                props[key] = parsed_value
            except DealWriteServiceError as exc:
                if self.strict_validation:
                    raise
                props[key] = raw_clean_value
                warnings.append(
                    {
                        "warning": "custom_column_cast_fallback",
                        "custom_column": key,
                        "expected_type": definition.data_type,
                        "value": raw_clean_value,
                        "message": exc.message,
                    }
                )
        return props, warnings, has_custom_columns

    @staticmethod
    def _coerce_custom_value(
        *,
        key: str,
        raw_value: str,
        definition: CustomFieldRuntime,
    ) -> object:
        data_type = (definition.data_type or "").strip().lower()
        if data_type == CRMCustomFieldDataType.TEXT.value:
            return raw_value
        if data_type == CRMCustomFieldDataType.SELECT.value:
            return raw_value
        if data_type == CRMCustomFieldDataType.MULTISELECT.value:
            return raw_value
        if data_type == CRMCustomFieldDataType.FORMULA.value:
            return raw_value
        if data_type == CRMCustomFieldDataType.INTEGER.value:
            try:
                return int(raw_value)
            except ValueError as exc:
                raise DealWriteServiceError(
                    f"Invalid integer for custom field '{key}': {raw_value}",
                    status_code=400,
                ) from exc
        if data_type == CRMCustomFieldDataType.NUMBER.value:
            try:
                return float(raw_value)
            except ValueError as exc:
                raise DealWriteServiceError(
                    f"Invalid number for custom field '{key}': {raw_value}",
                    status_code=400,
                ) from exc
        if data_type == CRMCustomFieldDataType.BOOLEAN.value:
            lowered = raw_value.strip().lower()
            if lowered in _CUSTOM_TRUE_VALUES:
                return True
            if lowered in _CUSTOM_FALSE_VALUES:
                return False
            raise DealWriteServiceError(
                f"Invalid boolean for custom field '{key}': {raw_value}",
                status_code=400,
            )
        if data_type == CRMCustomFieldDataType.DATE.value:
            parsed = DealImportProcessingService._parse_date(raw_value)
            if parsed is None:
                raise DealWriteServiceError(
                    f"Invalid date for custom field '{key}': {raw_value}",
                    status_code=400,
                )
            return parsed.isoformat()
        if data_type == CRMCustomFieldDataType.DATETIME.value:
            parsed = DealImportProcessingService._parse_date(raw_value)
            if parsed is None:
                raise DealWriteServiceError(
                    f"Invalid datetime for custom field '{key}': {raw_value}",
                    status_code=400,
                )
            return parsed.isoformat()
        if data_type == CRMCustomFieldDataType.CURRENCY.value:
            try:
                return DealImportProcessingService._parse_currency(raw_value)
            except ValueError as exc:
                raise DealWriteServiceError(
                    f"Invalid currency for custom field '{key}': {raw_value}",
                    status_code=400,
                ) from exc
        return raw_value

    @staticmethod
    def _parse_currency(value: str) -> float:
        normalized = re.sub(r"[$€£¥₹]", "", value)
        normalized = re.sub(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", "", normalized.upper())
        normalized = normalized.replace(",", "").replace(" ", "")
        if not normalized:
            raise ValueError("Empty currency value")
        return float(normalized)
