"""Service for processing bulk deal imports."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import col
from src.domain.models.master_tables.user_models import User as MasterUser
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMDealStatus,
    CRMPipeline,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)

_EMAIL_SPLIT_RE = re.compile(r"[;,]+")
_AMOUNT_CLEAN_RE = re.compile(r"[,$]")
_CUSTOM_PREFIX_RE = re.compile(r"^custom_(.+)$")
_VALID_DEAL_STATUSES = {s.value for s in CRMDealStatus}


@dataclass(slots=True)
class DealImportContext:
    integration_id: UUID
    pipeline_by_name: dict[str, CRMPipeline]
    stages_by_pipeline: dict[UUID, dict[str, CRMDealStage]]
    default_pipeline: CRMPipeline | None


class DealImportProcessingService:
    """Orchestrates CSV import for internal CRM deals."""

    def __init__(
        self,
        organisation_id: UUID,
        actor_user_id: UUID,
        storage_path: str,
        publisher: DealJobRedisPublisher,
    ) -> None:
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.storage_path = storage_path
        self.publisher = publisher

    async def process(self) -> dict:
        self.publisher.publish_progress("Downloading import file", "download", 0.05)
        content = await self._download_blob()

        self.publisher.publish_progress("Parsing import file", "parse", 0.15)
        rows = self._parse_csv(content)
        if not rows:
            raise DealWriteServiceError("Import file contains no rows", status_code=400)

        context = await self._load_context()
        user_map = await self._resolve_user_map(rows)

        return await self._process_rows(rows, context, user_map)

    async def _download_blob(self) -> bytes:
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            return await storage.get_blob_content(self.storage_path)
        finally:
            await storage.close()

    def _parse_csv(self, content: bytes) -> list[dict[str, str]]:
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        return [row for row in reader if any(self._clean_value(v) for v in row.values())]

    async def _load_context(self) -> DealImportContext:
        async for session in acquire_org_async_session(self.organisation_id):
            setup_service = InternalCRMSetupService(
                session, self.organisation_id, self.actor_user_id
            )
            integration = await setup_service.get_internal_integration()
            if not integration:
                raise DealWriteServiceError("Internal CRM is not configured", status_code=409)

            pipeline_stmt = (
                select(CRMPipeline)
                .where(
                    col(CRMPipeline.organisation_id) == self.organisation_id,
                    col(CRMPipeline.integration_id) == integration.integration_id,
                    col(CRMPipeline.is_deleted).is_(False),
                )
                .order_by(col(CRMPipeline.display_order).asc().nulls_last())
            )
            pipeline_result = await session.execute(pipeline_stmt)
            pipelines = pipeline_result.scalars().all()

            pipeline_by_name: dict[str, CRMPipeline] = {}
            for pipeline in pipelines:
                if pipeline.name:
                    pipeline_by_name[pipeline.name.lower()] = pipeline
                if pipeline.label:
                    pipeline_by_name[pipeline.label.lower()] = pipeline

            default_pipeline = next(
                (p for p in pipelines if p.is_default), pipelines[0] if pipelines else None
            )

            stage_stmt = select(CRMDealStage).where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.integration_id) == integration.integration_id,
                col(CRMDealStage.is_deleted).is_(False),
            )
            stage_result = await session.execute(stage_stmt)
            stages = stage_result.scalars().all()
            stages_by_pipeline: dict[UUID, dict[str, CRMDealStage]] = {}
            for stage in stages:
                pipeline_stages = stages_by_pipeline.setdefault(stage.pipeline_id, {})
                if stage.name:
                    pipeline_stages[stage.name.lower()] = stage
                if stage.label:
                    pipeline_stages[stage.label.lower()] = stage

            return DealImportContext(
                integration_id=integration.integration_id,
                pipeline_by_name=pipeline_by_name,
                stages_by_pipeline=stages_by_pipeline,
                default_pipeline=default_pipeline,
            )

        raise DealWriteServiceError("Failed to load import context", status_code=500)

    async def _resolve_user_map(self, rows: Iterable[dict[str, str]]) -> dict[str, UUID]:
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

    async def _process_rows(
        self,
        rows: list[dict[str, str]],
        context: DealImportContext,
        user_map: dict[str, UUID],
    ) -> dict:
        total_rows = len(rows)
        created: list[dict] = []
        failed: list[dict] = []
        warnings: list[dict] = []

        for idx, row in enumerate(rows, start=1):
            progress = 0.15 + (0.85 * (idx / total_rows))
            if idx == 1 or idx == total_rows or idx % 10 == 0:
                self.publisher.publish_progress(
                    f"Processing row {idx} of {total_rows}",
                    "create",
                    progress,
                )

            row_number = idx + 1  # account for header row
            try:
                payload, row_warnings = self._build_payload(row, context, user_map)
                warnings.extend({**w, "row_number": row_number} for w in row_warnings)

                deal = await self._create_deal(payload)
                created.append(
                    {
                        "row_number": row_number,
                        "deal_id": str(deal.deal_id),
                        "deal_name": deal.name,
                    }
                )
            except DealWriteServiceError as exc:
                failed.append(
                    {
                        "row_number": row_number,
                        "deal_name": self._clean_value(row.get("deal_name")),
                        "error": exc.message,
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "row_number": row_number,
                        "deal_name": self._clean_value(row.get("deal_name")),
                        "error": str(exc),
                    }
                )

        return {
            "total_rows": total_rows,
            "created_count": len(created),
            "failed_count": len(failed),
            "warning_count": len(warnings),
            "created": created,
            "failed": failed,
            "warnings": warnings,
        }

    def _build_payload(
        self,
        row: dict[str, str],
        context: DealImportContext,
        user_map: dict[str, UUID],
    ) -> tuple[dict, list[dict]]:
        warnings: list[dict] = []

        deal_name = self._clean_value(row.get("deal_name"))
        company_name = self._clean_value(row.get("company_name"))
        company_domain = self._clean_value(row.get("company_domain"))

        if not deal_name:
            deal_name = company_name or company_domain
        if not deal_name:
            raise DealWriteServiceError(
                "Missing deal_name (or company_name/company_domain fallback)", status_code=400
            )

        pipeline_name = self._clean_value(row.get("pipeline_name"))
        stage_name = self._clean_value(row.get("stage_name"))
        pipeline = None
        if pipeline_name:
            pipeline = context.pipeline_by_name.get(pipeline_name.lower())
            if not pipeline:
                raise DealWriteServiceError(f"Pipeline not found: {pipeline_name}", status_code=404)
        else:
            pipeline = context.default_pipeline

        stage = None
        if stage_name:
            if not pipeline:
                raise DealWriteServiceError("Stage provided without pipeline", status_code=400)
            stage_map = context.stages_by_pipeline.get(pipeline.pipeline_id, {})
            stage = stage_map.get(stage_name.lower())
            if not stage:
                raise DealWriteServiceError(f"Stage not found: {stage_name}", status_code=404)

        owner_email = self._clean_value(row.get("owner_email"))
        owner_user_id = None
        if owner_email:
            owner_user_id = user_map.get(owner_email.lower())
            if not owner_user_id:
                raise DealWriteServiceError(f"Owner not found: {owner_email}", status_code=404)

        team_emails = self._split_emails(row.get("team_member_emails"))
        team_members: list[dict] = []
        for email in team_emails:
            user_id = user_map.get(email.lower())
            if not user_id:
                warnings.append({"email": email, "warning": "team_member_not_found"})
                continue
            team_members.append({"user_id": user_id, "role": "member"})

        amount = self._parse_amount(self._clean_value(row.get("amount")))
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

        # Parse new first-class date fields
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

        # Extract custom properties (columns prefixed with custom_)
        custom_properties = self._extract_custom_properties(row)

        account_payload = {
            "domain": company_domain,
            "name": company_name,
            "website": self._clean_value(row.get("company_website")),
            "linkedin_url": self._clean_value(row.get("company_linkedin")),
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
            "owner_user_id": owner_user_id,
            "pipeline_id": pipeline.pipeline_id if pipeline else None,
            "stage_id": stage.stage_id if stage else None,
            "account": account_payload,
            "team_members": team_members or None,
            "custom_properties": custom_properties or None,
            "created_via": "import",
        }
        return payload, warnings

    async def _create_deal(self, payload: dict):
        async for org_session in acquire_org_async_session(self.organisation_id):
            async for master_session in acquire_master_async_session():
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=self.organisation_id,
                    actor_user_id=self.actor_user_id,
                )
                try:
                    deal = await service.create_deal(payload)
                    await org_session.commit()
                    await org_session.refresh(deal)
                    return deal
                except Exception:
                    await org_session.rollback()
                    raise

        raise DealWriteServiceError("Failed to create deal", status_code=500)

    @staticmethod
    def _clean_value(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
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
    def _split_emails(value: str | None) -> list[str]:
        if not value:
            return []
        raw = value.strip()
        if not raw:
            return []
        parts = [p.strip() for p in _EMAIL_SPLIT_RE.split(raw)]
        return [p for p in parts if p]

    @staticmethod
    def _extract_custom_properties(row: dict[str, str]) -> dict[str, str]:
        props: dict[str, str] = {}
        for col_name, raw_value in row.items():
            match = _CUSTOM_PREFIX_RE.match(col_name.strip())
            if not match:
                continue
            value = raw_value.strip() if raw_value else None
            if value:
                key = match.group(1)
                props[key] = value
        return props
