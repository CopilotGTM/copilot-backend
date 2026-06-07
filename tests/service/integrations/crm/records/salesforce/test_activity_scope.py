from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce import activity_scope


@pytest.mark.asyncio
async def test_initial_sync_deal_scope_filters_open_and_recent_closed_deals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    open_deal_id = uuid4()
    recent_closed_deal_id = uuid4()
    old_closed_deal_id = uuid4()
    executed_statements = []

    class FakeMappingHelper:
        def __init__(self, **kwargs) -> None:
            assert kwargs["organisation_id"] == organisation_id
            assert kwargs["integration_id"] == integration_id
            assert kwargs["provider"] == "salesforce"

        async def load_local_id_map(self, *, object_type, external_ids=None):
            assert object_type == CRMSourceObjectType.DEAL
            assert external_ids is None
            return {
                "006OPEN": open_deal_id,
                "006RECENT": recent_closed_deal_id,
                "006OLD": old_closed_deal_id,
            }

    class FakeSession:
        async def execute(self, statement):
            executed_statements.append(statement)
            return SimpleNamespace(all=lambda: [(open_deal_id,), (recent_closed_deal_id,)])

    async def fake_acquire_session(_organisation_id):
        assert _organisation_id == organisation_id
        yield FakeSession()

    monkeypatch.setattr(activity_scope, "CRMSourceMappingHelper", FakeMappingHelper)
    monkeypatch.setattr(activity_scope, "acquire_org_async_session", fake_acquire_session)

    external_ids = await activity_scope.load_initial_sync_deal_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    assert external_ids == ["006OPEN", "006RECENT"]
    sql = str(executed_statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "crm_deals.deal_status = 'open'" in sql
    assert "crm_deals.close_date >=" in sql
    assert "crm_deals.source_modified_at >=" in sql
    assert organisation_id.hex in sql
    assert integration_id.hex in sql


@pytest.mark.asyncio
async def test_initial_sync_deal_scope_returns_empty_when_no_deal_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    execute_called = False

    class FakeMappingHelper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def load_local_id_map(self, *, object_type, external_ids=None):
            return {}

    class FakeSession:
        async def execute(self, _statement):
            nonlocal execute_called
            execute_called = True
            return SimpleNamespace(all=lambda: [])

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    monkeypatch.setattr(activity_scope, "CRMSourceMappingHelper", FakeMappingHelper)
    monkeypatch.setattr(activity_scope, "acquire_org_async_session", fake_acquire_session)

    external_ids = await activity_scope.load_initial_sync_deal_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    assert external_ids == []
    assert execute_called is False
