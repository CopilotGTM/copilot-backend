import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.integrations.crm.orchestration.salesforce_cdc_repair_chain import (
    build_salesforce_cdc_repair_chain,
)
from src.workers.integrations_worker.tasks.crm.salesforce import salesforce_cdc_repair_tasks


def test_salesforce_cdc_repair_chain_is_serial_and_fenced() -> None:
    repair = build_salesforce_cdc_repair_chain(uuid4(), uuid4())

    assert [task["task"] for task in repair.tasks] == [
        "integrations.sync_salesforce_opportunity_schema",
        "integrations.sync_salesforce_pipelines",
        "integrations.sync_salesforce_users",
        "integrations.sync_salesforce_companies",
        "integrations.sync_salesforce_contacts",
        "integrations.sync_salesforce_deals",
        "integrations.sync_salesforce_tasks",
        "integrations.sync_salesforce_notes",
        "integrations.mark_salesforce_cdc_repair_succeeded",
    ]
    assert repair.options["link_error"]["task"] == (
        "integrations.mark_salesforce_cdc_repair_failed"
    )


def test_successful_repair_marks_pending_without_streaming_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeStateService:
        async def mark_repair_succeeded(self, **kwargs):
            calls.append(kwargs)
            return True

    async def fake_session(_organisation_id):
        yield SimpleNamespace()

    monkeypatch.setattr(salesforce_cdc_repair_tasks, "run_async", asyncio.run)
    monkeypatch.setattr(
        salesforce_cdc_repair_tasks,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(salesforce_cdc_repair_tasks, "acquire_org_async_session", fake_session)

    result = salesforce_cdc_repair_tasks.mark_salesforce_cdc_repair_succeeded_task.run(
        str(org_id), str(integration_id)
    )

    assert result == {"status": "pending"}
    assert calls[0]["channel"] == "/data/ChangeEvents"
