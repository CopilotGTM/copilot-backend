"""Salesforce tasks record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.tasks.task_normalizer import normalize_tasks
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    ActivityDimensionHit,
    TaskSyncResult,
)
from src.service.integrations.crm.records.salesforce.tasks.task_writer import SalesforceTaskWriter
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher import (
    SalesforceTasksFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceTaskSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceTaskSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce tasks record sync."""


class SalesforceTaskRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "task"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> TaskSyncResult:
        started_at = datetime.now(UTC)

        context = await self._load_context(organisation_id, integration_id)
        raw_tasks = await SalesforceTasksFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()
        normalized, skipped_malformed = normalize_tasks(raw_tasks)
        scoped = [task for task in normalized if self._is_in_scope(task)]
        all_external_ids = [task.external_id for task in scoped]

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=scoped,
            all_external_ids=all_external_ids,
        )

        completed_at = datetime.now(UTC)
        result = TaskSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_tasks=len(raw_tasks),
            tasks_created=write_result.tasks_created,
            tasks_updated=write_result.tasks_updated,
            tasks_reactivated=write_result.tasks_reactivated,
            tasks_deactivated=write_result.tasks_deactivated,
            unresolved_owner_link=write_result.unresolved_owner_link,
            unresolved_deal_link=write_result.unresolved_deal_link,
            unresolved_contact_link=write_result.unresolved_contact_link,
            unresolved_company_link=write_result.unresolved_company_link,
            skipped_malformed=skipped_malformed,
        )
        logger.info(
            "Salesforce tasks sync summary",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_tasks": result.fetched_tasks,
                "tasks_created": result.tasks_created,
                "tasks_updated": result.tasks_updated,
                "tasks_reactivated": result.tasks_reactivated,
                "tasks_deactivated": result.tasks_deactivated,
                "unresolved_owner_link": result.unresolved_owner_link,
                "unresolved_deal_link": result.unresolved_deal_link,
                "unresolved_contact_link": result.unresolved_contact_link,
                "unresolved_company_link": result.unresolved_company_link,
                "skipped_malformed": result.skipped_malformed,
            },
        )
        return result

    def _is_in_scope(self, task) -> bool:
        return (
            task.deal_external_id is not None
            or task.contact_external_id is not None
            or task.company_external_id is not None
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceTaskSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceTaskSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceTaskSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceTaskSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceTaskSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceTaskSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )
        raise SalesforceTaskSyncConfigurationError(
            "Could not acquire org session for Salesforce task sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized,
        all_external_ids,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = SalesforceTaskWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                all_external_ids=all_external_ids,
                user_map=await self._load_user_map(session, organisation_id, integration_id),
                deal_map=await self._load_deal_map(session, organisation_id, integration_id),
                contact_map=await self._load_contact_map(session, organisation_id, integration_id),
                company_map=await self._load_company_map(session, organisation_id, integration_id),
            )
            await session.commit()
            return write_result

        raise SalesforceTaskSyncConfigurationError(
            "Could not acquire org session for Salesforce task sync write"
        )

    async def _load_user_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            row.external_id: ActivityDimensionHit(
                internal_id=row.user_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.external_id is not None
        }

    async def _load_deal_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            row.external_id: ActivityDimensionHit(internal_id=row.deal_id, name=row.name)
            for row in rows
            if row.external_id is not None
        }

    async def _load_contact_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            row.external_id: ActivityDimensionHit(
                internal_id=row.contact_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.external_id is not None
        }

    async def _load_company_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            row.external_id: ActivityDimensionHit(internal_id=row.company_id, name=row.name)
            for row in rows
            if row.external_id is not None
        }
