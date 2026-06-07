"""Salesforce companies record sync orchestrator."""

from __future__ import annotations

from collections.abc import Iterable
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
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE as _DEFAULT_LOCAL_ID_QUERY_CHUNK_SIZE,
)
from src.service.integrations.crm.records.common.query_chunks import iter_chunks
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.salesforce.companies.company_normalizer import (
    normalize_accounts,
)
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    CompanySyncResult,
    CompanyWriteResult,
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

_COMPANY_PERSIST_CHUNK_SIZE = 2_000
_LOCAL_ID_QUERY_CHUNK_SIZE = _DEFAULT_LOCAL_ID_QUERY_CHUNK_SIZE


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
        logger.info(
            "Salesforce companies sync started",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
        )
        context = await self._load_context(organisation_id, integration_id)

        fetcher = SalesforceAccountsFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        write_result, skipped_malformed, fetched_companies, seen_external_ids = (
            CompanyWriteResult(),
            0,
            0,
            set[str](),
        )
        backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
        pending_raw: list[dict[str, Any]] = []

        logger.info(
            "Salesforce companies streaming persist started",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "chunk_size": _COMPANY_PERSIST_CHUNK_SIZE,
            },
        )
        async for page_records in fetcher.fetch_pages():
            fetched_companies += len(page_records)
            pending_raw.extend(page_records)
            if len(pending_raw) < _COMPANY_PERSIST_CHUNK_SIZE:
                continue
            chunk_result = await self._persist_raw_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_companies=pending_raw,
                seen_external_ids=seen_external_ids,
            )
            self._merge_write_result(write_result, chunk_result[0])
            skipped_malformed += chunk_result[1]
            self._merge_backfill_payload(backfill_payload, chunk_result[2])
            pending_raw = []

        if pending_raw:
            chunk_result = await self._persist_raw_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_companies=pending_raw,
                seen_external_ids=seen_external_ids,
            )
            self._merge_write_result(write_result, chunk_result[0])
            skipped_malformed += chunk_result[1]
            self._merge_backfill_payload(backfill_payload, chunk_result[2])

        deactivate_result = await self._deactivate_missing(
            organisation_id=organisation_id,
            integration_id=integration_id,
            seen_external_ids=seen_external_ids,
        )
        write_result.companies_deactivated += deactivate_result.companies_deactivated
        logger.info(
            "Salesforce companies persist completed",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_companies": fetched_companies,
                "seen_companies": len(seen_external_ids),
                "companies_created": write_result.companies_created,
                "companies_updated": write_result.companies_updated,
                "companies_reactivated": write_result.companies_reactivated,
                "companies_deactivated": write_result.companies_deactivated,
                "unresolved_owner_link": write_result.unresolved_owner_link,
                "deal_backfill_rows_updated": backfill_payload["rows_updated"],
                "skipped_malformed": skipped_malformed,
            },
        )

        completed_at = datetime.now(UTC)
        return CompanySyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_companies=fetched_companies,
            companies_created=write_result.companies_created,
            companies_updated=write_result.companies_updated,
            companies_reactivated=write_result.companies_reactivated,
            companies_deactivated=write_result.companies_deactivated,
            unresolved_owner_link=write_result.unresolved_owner_link,
            skipped_malformed=skipped_malformed,
            deal_backfill_rows_updated=backfill_payload["rows_updated"],
            deal_backfill_per_op=backfill_payload["per_op"],
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

    async def _persist_raw_chunk(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_companies: list[dict[str, Any]],
        seen_external_ids: set[str],
    ) -> tuple[CompanyWriteResult, int, dict[str, Any]]:
        normalized, skipped_malformed = normalize_accounts(raw_companies)
        seen_external_ids.update(company.external_id for company in normalized)
        logger.info(
            "Salesforce companies chunk persist started",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "raw_companies": len(raw_companies),
                "normalized_companies": len(normalized),
                "seen_companies": len(seen_external_ids),
            },
        )
        write_result, backfill_payload = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            deactivate_missing=False,
        )
        logger.info(
            "Salesforce companies chunk persist completed",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "companies_created": write_result.companies_created,
                "companies_updated": write_result.companies_updated,
                "companies_reactivated": write_result.companies_reactivated,
                "unresolved_owner_link": write_result.unresolved_owner_link,
                "skipped_malformed": skipped_malformed,
                "deal_backfill_rows_updated": backfill_payload["rows_updated"],
                "seen_companies": len(seen_external_ids),
            },
        )
        return write_result, skipped_malformed, backfill_payload

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
            owner_external_ids = _compact_str_set(
                company.owner_external_id for company in normalized
            )
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                external_ids=owner_external_ids,
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
                external_ids=all_external_ids,
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

    async def _deactivate_missing(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        seen_external_ids: set[str],
    ) -> CompanyWriteResult:
        async for session in acquire_org_async_session(organisation_id):
            writer = SalesforceCompanyWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=[],
                all_external_ids=seen_external_ids,
                owner_map={},
                deactivate_missing=True,
            )
            await session.commit()
            return write_result

        raise SalesforceCompanySyncConfigurationError(
            "Could not acquire org session for Salesforce companies deactivation"
        )

    @staticmethod
    def _merge_write_result(target: CompanyWriteResult, source: CompanyWriteResult) -> None:
        target.companies_created += source.companies_created
        target.companies_updated += source.companies_updated
        target.companies_reactivated += source.companies_reactivated
        target.companies_deactivated += source.companies_deactivated
        target.unresolved_owner_link += source.unresolved_owner_link

    @staticmethod
    def _merge_backfill_payload(target: dict[str, Any], source: dict[str, Any]) -> None:
        target["rows_updated"] += source["rows_updated"]
        per_op = target["per_op"]
        for op_name, rows_updated in source["per_op"].items():
            per_op[op_name] = per_op.get(op_name, 0) + rows_updated

    async def _load_external_to_pk(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: list[str] | None = None,
    ) -> dict[str, UUID]:
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.COMPANY,
            external_ids=external_ids,
            provider=self.PROVIDER,
        )
        return await _filter_existing_ids(
            session=session,
            model=CRMCompany,
            id_column=CRMCompany.company_id,
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_to_local=external_to_local,
        )

    async def _load_owner_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, UUID]:
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.USER,
            external_ids=external_ids,
            provider=self.PROVIDER,
        )
        return await _filter_existing_ids(
            session=session,
            model=CRMUser,
            id_column=CRMUser.user_id,
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_to_local=external_to_local,
        )


def _compact_str_set(values: Iterable[object]) -> set[str]:
    return {value for value in values if isinstance(value, str) and value}


async def _load_mapping_ids(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    provider: str,
    object_type: CRMSourceObjectType,
    external_ids: Iterable[str] | None = None,
) -> dict[str, UUID]:
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=provider,
    )
    return await helper.load_local_id_map(object_type=object_type, external_ids=external_ids)


async def _filter_existing_ids(
    *,
    session,
    model,
    id_column,
    organisation_id: UUID,
    integration_id: UUID,
    external_to_local: dict[str, UUID],
) -> dict[str, UUID]:
    if not external_to_local:
        return {}
    rows = []
    for local_ids in iter_chunks(set(external_to_local.values()), _LOCAL_ID_QUERY_CHUNK_SIZE):
        stmt = select(id_column).where(
            col(model.organisation_id) == organisation_id,
            col(model.integration_id) == integration_id,
            col(model.is_deleted) == False,  # noqa: E712
            col(id_column).in_(local_ids),
        )
        rows.extend((await session.execute(stmt)).all())
    live_ids = {local_id for (local_id,) in rows}
    return {
        external_id: local_id
        for external_id, local_id in external_to_local.items()
        if local_id in live_ids
    }
