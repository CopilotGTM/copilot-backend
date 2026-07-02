from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.utils.account_research_dispatch as dispatch_module
from src.domain.models.organisation_tables.job_models import DealJobType
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


@pytest.mark.asyncio
async def test_dispatch_account_research_queues_summary_when_provider_enriched(
    monkeypatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    account = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=company_id,
        name="Acme",
        domain="acme.com",
        company_enrichment={"provider": "apollo", "keywords": ["sales"]},
        company_enriched_at=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        last_researched_at=datetime(2026, 7, 2, 10, 1, tzinfo=UTC),
        account_summary=None,
    )
    added = []
    commits = 0

    async def fake_get(_model, _item_id):  # noqa: ANN001
        return account

    async def fake_execute(_stmt):  # noqa: ANN001
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def fake_add(row):  # noqa: ANN001
        added.append(row)

    async def fake_commit():
        nonlocal commits
        commits += 1

    session = SimpleNamespace(
        get=fake_get,
        execute=fake_execute,
        add=fake_add,
        commit=fake_commit,
    )

    async def fake_acquire_session(_organisation_id):
        yield session

    sent = []

    async def fake_send(task_name, **kwargs):  # noqa: ANN001
        sent.append((task_name, kwargs))

    class Projection:
        def __init__(self, _session, _organisation_id):  # noqa: ANN001
            pass

        async def upsert_account_enrichment_triggered(self, _account, **_kwargs):  # noqa: ANN001
            return None

    monkeypatch.setattr(dispatch_module, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", fake_send)
    monkeypatch.setattr(dispatch_module, "ActivityFeedProjectionService", Projection)

    job_id = await dispatch_module.dispatch_account_research_async(
        organisation_id=org_id,
        company_id=company_id,
        user_id=user_id,
    )

    assert job_id is not None
    assert len(added) == 1
    assert added[0].job_type == DealJobType.ACCOUNT_RESEARCH
    assert commits == 1
    assert sent[0][0] == "account_research.process_job"
