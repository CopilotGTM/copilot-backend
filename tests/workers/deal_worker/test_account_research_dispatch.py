from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.utils.account_research_dispatch as dispatch_module
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany


@pytest.mark.asyncio
async def test_dispatch_account_research_skips_name_only_account(monkeypatch) -> None:
    org_id = uuid4()
    company_id = uuid4()
    account = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=company_id,
        name="Acme",
        domain=None,
        website=None,
        linkedin_url=None,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=account))

    async def fake_acquire_session(_organisation_id):
        yield session

    send_task = AsyncMock()
    monkeypatch.setattr(dispatch_module, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", send_task)

    job_id = await dispatch_module.dispatch_account_research_async(
        organisation_id=org_id,
        company_id=company_id,
        user_id=uuid4(),
    )

    assert job_id is None
    session.get.assert_awaited_once()
    send_task.assert_not_awaited()
