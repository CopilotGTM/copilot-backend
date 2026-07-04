from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService


class _ScalarResult:
    def __init__(self, first=None, scalar=None):
        self._first = first
        self._scalar = scalar

    def scalars(self):
        return self

    def first(self):
        return self._first

    def scalar(self):
        return self._scalar


class _FakeMasterSession:
    def __init__(self, app_user: User):
        self.app_user = app_user

    async def execute(self, _stmt):
        return _ScalarResult(first=self.app_user)


class _FakeOrgSession:
    def __init__(self, *, internal_integration: KnowledgeIntegration, existing_user: CRMUser):
        self.internal_integration = internal_integration
        self.existing_user = existing_user
        self.added = []

    async def execute(self, stmt):
        table_text = str(stmt)
        where_text = str(getattr(stmt, "whereclause", ""))
        if "knowledge_integrations" in table_text:
            return _ScalarResult(first=self.internal_integration)
        if "crm_users" in table_text and "crm_users.integration_id" not in where_text:
            return _ScalarResult(first=self.existing_user)
        if "count" in table_text.lower():
            return _ScalarResult(scalar=0)
        return _ScalarResult(first=None)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_ensure_crm_user_reuses_existing_provider_created_row() -> None:
    organisation_id = uuid4()
    app_user_id = uuid4()
    provider_integration_id = uuid4()
    internal_integration = KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=organisation_id,
        name="Copilot CRM",
    )
    app_user = User(
        user_id=app_user_id,
        organisation_id=organisation_id,
        email="owner@example.com",
        first_name="Owner",
        last_name="User",
        supabase_user_id="supabase-user",
    )
    provider_user = CRMUser(
        user_id=app_user_id,
        organisation_id=organisation_id,
        integration_id=provider_integration_id,
        email="owner@example.com",
        full_name="Owner User",
        is_active=True,
        is_deleted=False,
    )
    org_session = _FakeOrgSession(
        internal_integration=internal_integration,
        existing_user=provider_user,
    )

    service = InternalCRMUserService(
        org_session=org_session,
        master_session=_FakeMasterSession(app_user),
        organisation_id=organisation_id,
        actor_user_id=app_user_id,
    )

    crm_user = await service.ensure_crm_user(app_user_id)

    assert crm_user is provider_user
    assert org_session.added == []
