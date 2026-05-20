"""HubSpot companies record sync orchestrator.

Fresh-session lifecycle:

  Session A  — load KnowledgeIntegration + IntegrationTokenInfo. Release.
  (no sess)  — paginated HubSpot companies fetch.
  (no sess)  — pure-Python normalise.
  Session B  — writer.apply() + backfill_crm_deals + commit.
"""

from __future__ import annotations

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
from src.service.integrations.crm.records.hubspot.companies.company_normalizer import (
    normalize_companies,
)
from src.service.integrations.crm.records.hubspot.companies.company_types import (
    CompanySyncResult,
    CompanyWriteResult,
    NormalizedHubSpotCompany,
)
from src.service.integrations.crm.records.hubspot.companies.company_writer import (
    HubSpotCompanyWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.companies_fetcher import (
    HubSpotCompaniesFetcher,
)

logger = get_logger(__name__)


class CompanySyncConfigurationError(ValueError):
    """Integration not usable for companies record sync (missing, wrong type, inactive)."""


class HubSpotCompanyRecordSyncService:
    """Orchestrate one HubSpot companies record sync run."""

    PROVIDER = "hubspot"
    OBJECT_TYPE = "company"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> CompanySyncResult:
        """Run the main execution path for this service or task."""

        started_at = datetime.now(UTC)

        token_info = await self._load_token_info(organisation_id, integration_id)

        raw_companies = await HubSpotCompaniesFetcher(token_info=token_info).fetch_all()

        normalized, skipped_malformed = normalize_companies(raw_companies)

        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
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
            companies_skipped_stale=write_result.companies_skipped_stale,
            unresolved_owner_link=write_result.unresolved_owner_link,
            skipped_malformed=skipped_malformed,
            deal_backfill_rows_updated=backfill_result["rows_updated"],
            deal_backfill_per_op=backfill_result["per_op"],
        )

    async def persist_raw_company(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_company: dict[str, Any],
    ) -> tuple[CompanyWriteResult, dict[str, Any]]:
        """Normalize and persist one HubSpot company snapshot from a webhook.

        A single webhook is not a full active snapshot, so this path upserts the
        target company and backfills only deals that reference that external id.
        """
        normalized, skipped_malformed = normalize_companies([raw_company])
        if not normalized:
            raise ValueError(
                f"HubSpot company webhook payload was malformed; skipped={skipped_malformed}"
            )
        return await self._persist_single(
            organisation_id=organisation_id,
            integration_id=integration_id,
            company=normalized[0],
        )

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise CompanySyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise CompanySyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise CompanySyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise CompanySyncConfigurationError(
            "Could not acquire org session for companies record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized: list[NormalizedHubSpotCompany],
    ) -> tuple[Any, dict[str, Any]]:
        all_external_ids = [c.external_id for c in normalized]
        async for session in acquire_org_async_session(organisation_id):
            # Load {owner_external_id → crm_users.user_id} ONCE for write-time
            # resolution of CRMCompany.owner_id. Same (org, integration) scope
            # as the writer; matches the chain's users-before-companies order.
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            writer = HubSpotCompanyWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                all_external_ids=all_external_ids,
                owner_map=owner_map,
            )

            # Reload the rows we just wrote to get the resolved company_id PKs
            # for the backfill ops. We do this in the same session so the flush
            # above is visible.
            company_map = await self._load_external_to_pk(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            # One deal backfill op: company_external_id → company_id (+ name + domain).
            # FK+name+label shape.
            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            mapping: dict[str, tuple[UUID, str | None, str | None, None]] = {}
            for company in normalized:
                if company.external_id in write_result.skipped_stale_external_ids:
                    continue
                pk = company_map.get(company.external_id)
                if pk is None:
                    # Writer just inserted/updated this row and session flushed —
                    # an absence here means the select couldn't see it, which is
                    # a framework bug. Skip defensively rather than raising.
                    continue
                mapping[company.external_id] = (
                    pk,
                    company.name,
                    company.domain,
                    None,
                )

            if mapping:
                ops = [
                    DealFkBackfillOp(
                        external_col="company_external_id",
                        fk_col="company_id",
                        name_col="company_name",
                        label_col="company_domain",
                        mapping=mapping,
                    ),
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

        raise CompanySyncConfigurationError(
            "Could not acquire org session for companies record sync write"
        )

    async def _persist_single(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        company: NormalizedHubSpotCompany,
    ) -> tuple[CompanyWriteResult, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            writer = HubSpotCompanyWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply_single(company=company, owner_map=owner_map)

            if company.external_id in write_result.skipped_stale_external_ids:
                await session.commit()
                return write_result, {"rows_updated": 0, "per_op": {}}

            # Reload after flush so the webhook-created/reactivated company_id is
            # available for deals that only had company_external_id.
            company_map = await self._load_external_to_pk(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            company_pk = company_map.get(company.external_id)
            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            if company_pk is not None:
                backfill_result = await backfill_crm_deals(
                    session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    ops=[
                        DealFkBackfillOp(
                            external_col="company_external_id",
                            fk_col="company_id",
                            name_col="company_name",
                            label_col="company_domain",
                            mapping={
                                company.external_id: (
                                    company_pk,
                                    company.name,
                                    company.domain,
                                    None,
                                )
                            },
                        )
                    ],
                )
                backfill_payload["rows_updated"] = backfill_result.rows_updated
                backfill_payload["per_op"] = dict(backfill_result.per_op)

            await session.commit()
            return write_result, backfill_payload

        raise CompanySyncConfigurationError(
            "Could not acquire org session for single company record sync write"
        )

    async def _load_external_to_pk(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.companies import (
            CRMCompany,
        )

        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.company_id for r in rows if r.external_id is not None}

    async def _load_owner_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        """`{owner_external_id → crm_users.user_id}` for write-time owner_id resolution."""
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.user_id for r in rows if r.external_id is not None}
