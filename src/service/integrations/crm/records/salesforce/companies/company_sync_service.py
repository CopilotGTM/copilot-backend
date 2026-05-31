"""Salesforce companies record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.salesforce.companies.company_normalizer import (
    normalize_accounts,
)
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    CompanySyncResult,
    NormalizedSalesforceCompany,
)
from src.service.integrations.crm.records.salesforce.companies.company_writer import (
    SalesforceCompanyWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher import (
    SalesforceAccountsFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceCompanySyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceCompanySyncConfigurationError(ValueError):
    """Integration not usable for Salesforce companies record sync."""


class SalesforceCompanyRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "company"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> CompanySyncResult:
        started_at = datetime.now(UTC)
        context = await self._load_context(organisation_id, integration_id)

        raw_companies = await SalesforceAccountsFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()
        normalized, skipped_malformed = normalize_accounts(raw_companies)

        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            deactivate_missing=True,
        )

        completed_at = datetime.now(UTC)
        return CompanySyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_companies=len(raw_companies),
            companies_created=write_result.companies_created,
            companies_updated=write_result.companies_updated,
            companies_reactivated=write_result.companies_reactivated,
            companies_deactivated=write_result.companies_deactivated,
            unresolved_owner_link=write_result.unresolved_owner_link,
            skipped_malformed=skipped_malformed,
            deal_backfill_rows_updated=backfill_result["rows_updated"],
            deal_backfill_per_op=backfill_result["per_op"],
        )

    async def persist_single_account(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_account: dict[str, Any],
    ) -> dict[str, Any]:
        normalized, skipped_malformed = normalize_accounts([raw_account])
        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            deactivate_missing=False,
        )
        return {
            "companies_created": write_result.companies_created,
            "companies_updated": write_result.companies_updated,
            "companies_reactivated": write_result.companies_reactivated,
            "companies_deactivated": write_result.companies_deactivated,
            "unresolved_owner_link": write_result.unresolved_owner_link,
            "skipped_malformed": skipped_malformed,
            "deal_backfill_rows_updated": backfill_result["rows_updated"],
            "deal_backfill_per_op": backfill_result["per_op"],
        }

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceCompanySyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceCompanySyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceCompanySyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceCompanySyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceCompanySyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceCompanySyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )

        raise SalesforceCompanySyncConfigurationError(
            "Could not acquire org session for Salesforce companies record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized: list[NormalizedSalesforceCompany],
        deactivate_missing: bool = True,
    ) -> tuple[Any, dict[str, Any]]:
        all_external_ids = [company.external_id for company in normalized]
        async for session in acquire_org_async_session(organisation_id):
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            writer = SalesforceCompanyWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                all_external_ids=all_external_ids,
                owner_map=owner_map,
                deactivate_missing=deactivate_missing,
            )

            company_map = await self._load_external_to_pk(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            mapping: dict[str, tuple[UUID, str | None, str | None, None]] = {}
            for company in normalized:
                pk = company_map.get(company.external_id)
                if pk is None:
                    continue
                mapping[company.external_id] = (pk, company.name, company.domain, None)

            if mapping:
                ops = [
                    DealFkBackfillOp(
                        external_col="company_external_id",
                        fk_col="company_id",
                        name_col="company_name",
                        label_col="company_domain",
                        mapping=mapping,
                    )
                ]
                backfill_result = await backfill_crm_deals(
                    session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    ops=ops,
                )
                backfill_payload["rows_updated"] = backfill_result.rows_updated
                backfill_payload["per_op"] = dict(backfill_result.per_op)

            await session.commit()
            return write_result, backfill_payload

        raise SalesforceCompanySyncConfigurationError(
            "Could not acquire org session for Salesforce companies record sync write"
        )

    async def _load_external_to_pk(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany

        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {row.external_id: row.company_id for row in rows if row.external_id is not None}

    async def _load_owner_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {row.external_id: row.user_id for row in rows if row.external_id is not None}
