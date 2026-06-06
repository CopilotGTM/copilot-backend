"""HubSpot tasks record sync orchestrator."""

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
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.tasks.task_normalizer import normalize_tasks
from src.service.integrations.crm.records.hubspot.tasks.task_types import (
    ActivityDimensionHit,
    TaskSyncResult,
    TaskWriteResult,
)
from src.service.integrations.crm.records.hubspot.tasks.task_writer import HubSpotTaskWriter
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.tasks_fetcher import HubSpotTasksFetcher

logger = get_logger(__name__)


@dataclass(slots=True)
class HubSpotTaskSyncContext:
    """Provider token context loaded from an org-scoped HubSpot integration row."""

    token_info: IntegrationTokenInfo


class TaskSyncConfigurationError(ValueError):
    """Integration not usable for HubSpot tasks record sync."""


class HubSpotTaskRecordSyncService:
    """Coordinate full and single-record HubSpot task writes.

    Full sync callers fetch every task and may deactivate missing rows. The
    incremental repair path calls `persist_raw_task()` with one full snapshot,
    which deliberately avoids deactivating unrelated activities.
    """

    PROVIDER = "hubspot"
    OBJECT_TYPE = "task"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> TaskSyncResult:
        """Fetch all HubSpot tasks for an integration and persist an active snapshot."""
        started_at = datetime.now(UTC)

        # Load credentials in a short DB session, then release it before the
        # provider scan so a slow HubSpot page cannot hold a tenant connection.
        context = await self._load_context(organisation_id, integration_id)
        raw_tasks = await HubSpotTasksFetcher(token_info=context.token_info).fetch_all()
        normalized, skipped_malformed = normalize_tasks(raw_tasks)
        all_external_ids = [task.external_id for task in normalized]

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
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
            "HubSpot tasks sync summary",
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

    async def persist_raw_task(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_task: dict,
    ):
        """Normalize and persist one HubSpot task snapshot from incremental repair."""
        normalized, skipped_malformed = normalize_tasks([raw_task])
        if not normalized:
            logger.warning(
                "HubSpot task incremental payload was malformed",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "skipped_malformed": skipped_malformed,
                },
            )
            return TaskWriteResult()

        return await self._persist_single(
            organisation_id=organisation_id,
            integration_id=integration_id,
            task=normalized[0],
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> HubSpotTaskSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            # The organisation predicate is the tenant trust boundary; queued
            # worker payloads must not resolve integrations by external id alone.
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise TaskSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise TaskSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise TaskSyncConfigurationError(f"Integration {integration_id} is not active")
            return HubSpotTaskSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id)
            )
        raise TaskSyncConfigurationError("Could not acquire org session for HubSpot task sync")

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized,
        all_external_ids,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotTaskWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            # Dimension maps are loaded inside the same org session as the
            # writer so task associations cannot link across tenant schemas.
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

        raise TaskSyncConfigurationError(
            "Could not acquire org session for HubSpot task sync write"
        )

    async def _persist_single(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        task,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotTaskWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            # Single-record writes come from webhooks/incremental polling. They
            # reuse the same association maps as full sync but do not mark other
            # tasks deleted just because they were absent from this one payload.
            write_result = await writer.apply_single(
                task=task,
                user_map=await self._load_user_map(session, organisation_id, integration_id),
                deal_map=await self._load_deal_map(session, organisation_id, integration_id),
                contact_map=await self._load_contact_map(session, organisation_id, integration_id),
                company_map=await self._load_company_map(session, organisation_id, integration_id),
            )
            await session.commit()
            return write_result

        raise TaskSyncConfigurationError(
            "Could not acquire org session for HubSpot task sync write"
        )

    async def _load_user_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.USER
        )
        if not external_to_local:
            return {}
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.user_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.user_id]: ActivityDimensionHit(
                internal_id=row.user_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.user_id in local_to_external
        }

    async def _load_deal_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.DEAL
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
            col(CRMDeal.deal_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.deal_id]: ActivityDimensionHit(
                internal_id=row.deal_id, name=row.name
            )
            for row in rows
            if row.deal_id in local_to_external
        }

    async def _load_contact_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.CONTACT
        )
        if not external_to_local:
            return {}
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.contact_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.contact_id]: ActivityDimensionHit(
                internal_id=row.contact_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.contact_id in local_to_external
        }

    async def _load_company_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.COMPANY
        )
        if not external_to_local:
            return {}
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.company_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.company_id]: ActivityDimensionHit(
                internal_id=row.company_id, name=row.name
            )
            for row in rows
            if row.company_id in local_to_external
        }

    async def _load_external_to_local(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        object_type: CRMSourceObjectType,
    ) -> dict[str, UUID]:
        helper = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
        )
        return await helper.load_local_id_map(object_type=object_type)


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}
