"""Salesforce Opportunity schema sync orchestrator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldEntity
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.common.registry_writer import CRMRegistryWriter
from src.service.integrations.crm.schema.common.types import (
    CanonicalMatch,
    NormalizedSchemaField,
    ProviderCustomMatch,
    RegistryWriteResult,
    SchemaSyncResult,
)
from src.service.integrations.crm.schema.salesforce.opportunities.classifier import (
    MalformedSalesforceField,
    classify_salesforce_opportunity_field,
)
from src.service.integrations.crm.schema.salesforce.opportunities.normalizer import (
    normalize_salesforce_field,
    salesforce_options_payload,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.salesforce.schema_fetcher import (
    SalesforceSchemaFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceSchemaSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceSchemaSyncConfigurationError(ValueError):
    """Raised when the integration cannot be used for Salesforce schema sync."""


class SalesforceOpportunitySchemaSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "opportunity"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SchemaSyncResult:
        started_at = datetime.now(UTC)
        context = await self._load_context(organisation_id, integration_id)

        raw_fields = await SalesforceSchemaFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_fields(object_type="Opportunity")

        canonical_count = 0
        skipped_count = 0
        normalized: list[NormalizedSchemaField] = []
        for raw in raw_fields:
            try:
                classification = classify_salesforce_opportunity_field(raw)
            except MalformedSalesforceField:
                skipped_count += 1
                logger.warning(
                    "Skipping malformed Salesforce field",
                    extra={
                        "integration_id": str(integration_id),
                        "raw_keys": list(raw.keys()) if isinstance(raw, Mapping) else None,
                    },
                )
                continue

            if isinstance(classification, CanonicalMatch):
                canonical_count += 1
                continue

            assert isinstance(classification, ProviderCustomMatch)
            normalized.append(normalize_salesforce_field(raw, classification))

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            fields=normalized,
        )

        completed_at = datetime.now(UTC)
        return SchemaSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            fetched_properties=len(raw_fields),
            canonical_properties=canonical_count,
            custom_properties_persisted=len(normalized),
            created=write_result.created,
            updated=write_result.updated,
            reactivated=write_result.reactivated,
            deactivated=write_result.deactivated,
            skipped=skipped_count,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceSchemaSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceSchemaSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceSchemaSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceSchemaSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )

            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceSchemaSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )

            return SalesforceSchemaSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )

        raise SalesforceSchemaSyncConfigurationError(
            "Could not acquire org session for Salesforce schema sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        async for session in acquire_org_async_session(organisation_id):
            writer = CRMRegistryWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                entity_type=CRMCustomFieldEntity.DEAL,
                provider_name=self.PROVIDER,
                options_serializer=salesforce_options_payload,
                log_label="Salesforce Opportunity",
            )
            result = await writer.apply(fields=fields)
            await session.commit()
            return result
        raise SalesforceSchemaSyncConfigurationError(
            "Could not acquire org session for Salesforce schema registry write"
        )
