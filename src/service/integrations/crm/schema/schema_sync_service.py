"""HubSpot deal schema sync orchestrator.

Fresh-session pattern:
  Session A  — load integration, extract IntegrationTokenInfo, release.
  (no session)  — HubSpot Properties API fetch with token retry.
  (no session)  — pure-Python classify + normalize pass.
  Session B  — registry writer upsert/deactivate, commit, release.

The Tier 3 Celery task that wraps this service owns sync-history and
connection-metadata sessions (see tasks/crm/hubspot_deal_schema_task.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.classifier import (
    MalformedHubSpotProperty,
    classify,
)
from src.service.integrations.crm.schema.normalizer import normalize
from src.service.integrations.crm.schema.registry_writer import (
    HubSpotDealRegistryWriter,
)
from src.service.integrations.crm.schema.schema_types import (
    CanonicalMatch,
    IgnoredMatch,
    NormalizedSchemaField,
    ProviderCustomMatch,
    RegistryWriteResult,
    SchemaSyncResult,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.schema_fetcher import (
    HubSpotSchemaFetcher,
)

logger = get_logger(__name__)


class SchemaSyncConfigurationError(ValueError):
    """Raised when the integration cannot be used for schema sync (not HubSpot, inactive, missing)."""


class HubSpotDealSchemaSyncService:
    PROVIDER = "hubspot"
    OBJECT_TYPE = "deal"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SchemaSyncResult:
        """Run one full HubSpot deal schema sync and return a compact summary."""

        started_at = datetime.now(UTC)
        token_info = await self._load_token_info(organisation_id, integration_id)

        # External call happens before DB session B is opened to avoid connection
        # pressure during HubSpot API latency.
        raw_fields = await HubSpotSchemaFetcher(token_info=token_info).fetch_properties(
            object_type="deals"
        )

        canonical_count = 0
        skipped_count = 0
        normalized: list[NormalizedSchemaField] = []
        for raw in raw_fields:
            try:
                classification = classify(raw)
            except MalformedHubSpotProperty:
                skipped_count += 1
                logger.warning(
                    "Skipping malformed HubSpot property",
                    extra={
                        "integration_id": str(integration_id),
                        "raw_keys": list(raw.keys()) if isinstance(raw, Mapping) else None,
                    },
                )
                continue

            if isinstance(classification, CanonicalMatch):
                canonical_count += 1
                continue
            if isinstance(classification, IgnoredMatch):
                skipped_count += 1
                continue

            assert isinstance(classification, ProviderCustomMatch)
            normalized.append(normalize(raw, classification))

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

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        """Validate integration state and return token context for HubSpot calls."""

        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SchemaSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise SchemaSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise SchemaSyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise SchemaSyncConfigurationError("Could not acquire org session for schema sync")

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        """Persist normalized fields in a dedicated write session."""

        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotDealRegistryWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(fields=fields)
            await session.commit()
            return write_result
        raise SchemaSyncConfigurationError(
            "Could not acquire org session for schema registry write"
        )
