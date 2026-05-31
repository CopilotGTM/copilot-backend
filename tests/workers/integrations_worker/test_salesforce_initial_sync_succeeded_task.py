from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.workers.integrations_worker.tasks.crm.salesforce import (
    salesforce_initial_sync_succeeded_task,
)


def _run_coroutine(coro):
    return asyncio.run(coro)


def test_salesforce_initial_sync_success_enqueues_all_enabled_cdc_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    queued: list[dict[str, object]] = []
    integration = SimpleNamespace(
        organisation_id=org_id,
        integration_id=integration_id,
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "opportunity": {"enabled": True, "channel": "/data/OpportunityChangeEvent"},
                "account": {"enabled": True, "channel": "/data/AccountChangeEvent"},
                "contact": {"enabled": True, "channel": "/data/ContactChangeEvent"},
                "user": {"enabled": True, "channel": "/data/UserChangeEvent"},
                "task": {"enabled": True, "channel": "/data/TaskChangeEvent"},
                "content_note": {"enabled": True, "channel": "/data/ContentNoteChangeEvent"},
                "content_document_link": {
                    "enabled": True,
                    "channel": "/data/ContentDocumentLinkChangeEvent",
                },
            },
        },
        last_synced_at=None,
    )

    class FakeScalarResult:
        def scalar_one_or_none(self):
            return integration

    fake_session = SimpleNamespace(
        execute=AsyncMock(return_value=FakeScalarResult()),
        add=lambda _integration: None,
        commit=AsyncMock(),
    )

    async def fake_acquire_session(_organisation_id):
        yield fake_session

    async def fake_send_task(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(salesforce_initial_sync_succeeded_task, "run_async", _run_coroutine)
    monkeypatch.setattr(
        salesforce_initial_sync_succeeded_task,
        "acquire_org_async_session",
        fake_acquire_session,
    )
    monkeypatch.setattr(
        salesforce_initial_sync_succeeded_task,
        "send_integrations_task_async",
        fake_send_task,
    )

    result = salesforce_initial_sync_succeeded_task.mark_salesforce_initial_sync_succeeded_task.run(
        str(org_id),
        str(integration_id),
    )

    assert result["status"] == "success"
    assert [entry["kwargs"]["channel"] for entry in queued] == [
        "/data/OpportunityChangeEvent",
        "/data/AccountChangeEvent",
        "/data/ContactChangeEvent",
        "/data/UserChangeEvent",
        "/data/TaskChangeEvent",
        "/data/ContentNoteChangeEvent",
        "/data/ContentDocumentLinkChangeEvent",
    ]
