from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.users import user_tenant_setup_service as service_module
from src.service.users.user_tenant_setup_service import (
    TenantUserSnapshot,
    UserTenantSetupService,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _Session:
    def __init__(self):
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement):
        existing = next((item for item in self.added if isinstance(item, CRMUser)), None)
        return _ScalarResult(existing)

    def add(self, item: object) -> None:
        if item not in self.added:
            self.added.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_ensure_user_creates_one_crm_user_and_is_safe_to_repeat(monkeypatch) -> None:
    organisation_id = uuid4()
    user_id = uuid4()
    integration_id = uuid4()
    session = _Session()
    setup_calls = []

    class _Setup:
        def __init__(self, _session, org_id, actor_id):
            setup_calls.append((org_id, actor_id))

        async def setup_internal_crm(self, *, commit: bool):
            assert commit is False
            return SimpleNamespace(integration=SimpleNamespace(integration_id=integration_id))

    monkeypatch.setattr(service_module, "InternalCRMSetupService", _Setup)
    snapshot = TenantUserSnapshot(
        user_id=user_id,
        organisation_id=organisation_id,
        email="founder@acme.com",
        first_name="Founding",
        last_name="Admin",
        user_role="admin",
        job_title="Founder",
        is_active=True,
    )
    service = UserTenantSetupService(session)

    first = await service.ensure_user(snapshot)
    second = await service.ensure_user(snapshot)

    crm_users = [item for item in session.added if isinstance(item, CRMUser)]
    assert len(crm_users) == 1
    assert crm_users[0].user_id == user_id
    assert first.created is True
    assert second.created is False
    assert setup_calls == [(organisation_id, user_id), (organisation_id, user_id)]
