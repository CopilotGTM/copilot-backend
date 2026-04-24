"""Salesforce Opportunity pipelines + stages record-sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_normalizer import (
    normalize_salesforce_pipelines,
)
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_types import (
    PipelineSyncResult,
)
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_writer import (
    SalesforcePipelineWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher import (
    SalesforcePipelinesFetcher,
)


@dataclass(slots=True)
class SalesforcePipelineSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforcePipelineSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce pipelines record sync."""


class SalesforcePipelineRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "pipeline"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> PipelineSyncResult:
        started_at = datetime.now(UTC)
        context = await self._load_context(organisation_id, integration_id)

        fetcher = SalesforcePipelinesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        raw_record_types = await fetcher.fetch_opportunity_record_types()
        selected_record_types = _select_record_types_for_sync(raw_record_types)
        stage_values_by_record_type = {
            record_type["Id"]: await fetcher.fetch_stage_values_for_record_type(record_type["Id"])
            for record_type in selected_record_types
            if isinstance(record_type.get("Id"), str) and record_type["Id"]
        }
        raw_opportunity_stages = await fetcher.fetch_opportunity_stage_metadata()

        normalized, skipped_record_types, skipped_stages, unresolved_stage_metadata = (
            normalize_salesforce_pipelines(
                record_types=selected_record_types,
                stage_values_by_record_type=stage_values_by_record_type,
                opportunity_stages=raw_opportunity_stages,
            )
        )

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            pipelines=normalized,
        )

        completed_at = datetime.now(UTC)
        return PipelineSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_record_types=len(selected_record_types),
            fetched_stage_values=sum(
                len(values) for values in stage_values_by_record_type.values()
            ),
            pipelines_created=write_result.pipelines_created,
            pipelines_updated=write_result.pipelines_updated,
            pipelines_reactivated=write_result.pipelines_reactivated,
            pipelines_deactivated=write_result.pipelines_deactivated,
            stages_created=write_result.stages_created,
            stages_updated=write_result.stages_updated,
            stages_reactivated=write_result.stages_reactivated,
            stages_deactivated=write_result.stages_deactivated,
            skipped_record_types=skipped_record_types,
            skipped_stages=skipped_stages,
            unresolved_stage_metadata=unresolved_stage_metadata,
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforcePipelineSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforcePipelineSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforcePipelineSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforcePipelineSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforcePipelineSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforcePipelineSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )

        raise SalesforcePipelineSyncConfigurationError(
            "Could not acquire org session for Salesforce pipelines record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        pipelines: list,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = SalesforcePipelineWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(pipelines=pipelines)
            await session.commit()
            return write_result

        raise SalesforcePipelineSyncConfigurationError(
            "Could not acquire org session for Salesforce pipelines write"
        )


def _select_record_types_for_sync(record_types: list[dict]) -> list[dict]:
    return [row for row in record_types if bool(row.get("IsActive", True))]
