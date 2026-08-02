"""Shared founder tenant and storage provisioning for signup workers."""

from uuid import UUID

from src.infra.database.sessions import acquire_org_async_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.service.users.user_tenant_setup_service import (
    TenantUserSnapshot,
    UserTenantSetupService,
)


class OrganisationProvisioningService:
    """Provision user-owned tenant defaults before the required storage gate."""

    async def provision_founder(
        self,
        organisation_id: UUID,
        founder: TenantUserSnapshot,
    ) -> None:
        """Ensure founder CRM state, release the DB session, then create storage."""
        if founder.organisation_id != organisation_id:
            raise ValueError("Founder does not belong to the organisation")

        # The tenant session is released before Azure I/O so a slow provider
        # response cannot pin a pooled database connection.
        await self.provision_founder_tenant(organisation_id, founder)
        await self.provision_storage(organisation_id)

    async def provision_founder_tenant(
        self,
        organisation_id: UUID,
        founder: TenantUserSnapshot,
    ) -> None:
        """Ensure founder-owned tenant defaults without holding a master session."""
        if founder.organisation_id != organisation_id:
            raise ValueError("Founder does not belong to the organisation")
        sessions = acquire_org_async_session(organisation_id)
        try:
            org_session = await anext(sessions)
            await UserTenantSetupService(org_session).ensure_user(founder)
        finally:
            await sessions.aclose()

    async def provision_storage(self, organisation_id: UUID) -> None:
        """Create the required Azure container after tenant work has committed."""
        # Storage completion is the final external gate; failures propagate so
        # the worker can persist a retryable reconciliation state.
        async with AzureOrganisationBlobStorage(organisation_id) as storage:
            await storage.provision_container()
