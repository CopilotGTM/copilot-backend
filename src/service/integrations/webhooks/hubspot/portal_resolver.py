"""Resolve a HubSpot portal id to tenant-scoped integrations.

HubSpot sends `portalId` on each webhook, but not our organisation or
integration ids. This resolver scans approved active organisations and yields
only integrations whose stored HubSpot metadata matches that portal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session


@dataclass(frozen=True, slots=True)
class HubSpotPortalIntegration:
    """Minimal routing target needed by downstream webhook handlers."""

    organisation_id: UUID
    integration_id: UUID


def integration_matches_hubspot_portal(integration: object, portal_id: str) -> bool:
    """Return whether an integration's HubSpot metadata matches the webhook portal."""
    metadata = getattr(integration, "connection_metadata", None) or {}
    return str(metadata.get("hubspot_portal_id") or "") == str(portal_id)


async def iter_hubspot_integrations_for_portal(
    portal_id: str,
) -> AsyncIterator[HubSpotPortalIntegration]:
    """Yield active HubSpot integrations that belong to the webhook portal."""
    organisation_ids = await _list_active_organisation_ids()
    for organisation_id in organisation_ids:
        async for org_session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                # Portal id is provider-controlled metadata. The org schema and
                # integration type filters keep the scan inside approved tenants.
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            result = await org_session.execute(statement)
            for integration in result.scalars().all():
                if integration_matches_hubspot_portal(integration, portal_id):
                    yield HubSpotPortalIntegration(
                        organisation_id=organisation_id,
                        integration_id=integration.integration_id,
                    )


async def _list_active_organisation_ids() -> list[UUID]:
    """Return organisations that are eligible to receive webhook writes."""
    async for master_session in acquire_master_async_session():
        statement = select(col(Organisation.organisation_id)).where(
            col(Organisation.is_active).is_(True),
            col(Organisation.status) == OrganisationStatus.APPROVED,
        )
        result = await master_session.execute(statement)
        return list(result.scalars().all())
    return []
