from uuid import uuid4

import pytest
from src.service.orgnisation import organisation_provisioning_service as service_module
from src.service.orgnisation.organisation_provisioning_service import (
    OrganisationProvisioningService,
)
from src.service.users.user_tenant_setup_service import TenantUserSnapshot


@pytest.mark.asyncio
async def test_founder_tenant_setup_finishes_before_storage(monkeypatch) -> None:
    events: list[str] = []
    organisation_id = uuid4()
    founder = TenantUserSnapshot(
        user_id=uuid4(),
        organisation_id=organisation_id,
        email="founder@acme.com",
        first_name="Founding",
        last_name="Admin",
        user_role="admin",
        job_title=None,
        is_active=True,
    )

    async def acquire_session(_organisation_id):
        try:
            yield object()
        finally:
            events.append("tenant-released")

    class _UserSetup:
        def __init__(self, _session):
            pass

        async def ensure_user(self, snapshot):
            assert snapshot is founder
            events.append("tenant-user")

    class _Storage:
        def __init__(self, value):
            assert value == organisation_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def provision_container(self):
            events.append("storage")

    monkeypatch.setattr(service_module, "acquire_org_async_session", acquire_session)
    monkeypatch.setattr(service_module, "UserTenantSetupService", _UserSetup)
    monkeypatch.setattr(service_module, "AzureOrganisationBlobStorage", _Storage)

    await OrganisationProvisioningService().provision_founder(organisation_id, founder)

    assert events == ["tenant-user", "tenant-released", "storage"]
