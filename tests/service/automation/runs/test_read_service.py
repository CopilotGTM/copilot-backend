"""Owner-scoped run-history service contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.runs.read_service import AgentRunReadError, AgentRunReadService


def run(*, organisation_id: UUID, owner_user_id: UUID) -> AgentRun:
    return AgentRun(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        agent_id=uuid4(),
        trigger_id=uuid4(),
        agent_revision=2,
        status=AgentRunStatus.SUCCEEDED,
        source_domain="crm",
        event_type="crm.deal.stage_changed",
        source_event_id=str(uuid4()),
        agent_snapshot={"prompt": "Coach deal"},
        trigger_snapshot={"task_config": {}},
        input_snapshot={"deal": {"deal_id": str(uuid4())}},
        output_text="Call buyer",
        attempt_count=1,
        created_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )


class FakeAccess:
    def __init__(self) -> None:
        self.rows = []
        self.item = None
        self.list_call = None

    async def list_runs(self, **kwargs):
        self.list_call = kwargs
        return self.rows

    async def get_run(self, _run_id):
        return self.item


def service():
    organisation_id = uuid4()
    owner_user_id = uuid4()
    item = run(organisation_id=organisation_id, owner_user_id=owner_user_id)
    access = FakeAccess()
    result = AgentRunReadService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        owner_user_id,
        access=access,
    )
    return result, access, item


@pytest.mark.asyncio
async def test_list_returns_compact_run_metadata_and_cursor_page() -> None:
    read, access, item = service()
    access.rows = [item]

    page = await read.list_runs(
        agent_id=item.agent_id,
        trigger_id=None,
        status=AgentRunStatus.SUCCEEDED,
        cursor=None,
        limit=20,
    )

    assert page.items[0].run_id == item.run_id
    assert page.items[0].output_preview == "Call buyer"
    assert page.items[0].attempt_count == 1
    assert page.next_cursor is None
    assert access.list_call["limit"] == 21


@pytest.mark.asyncio
async def test_detail_returns_snapshots_and_usage_metadata() -> None:
    read, access, item = service()
    access.item = item

    detail = await read.get_run(item.run_id)

    assert detail.agent_snapshot == {"prompt": "Coach deal"}
    assert detail.input_snapshot["deal"]["deal_id"]


@pytest.mark.asyncio
async def test_detail_hides_foreign_or_missing_run_as_not_found() -> None:
    read, _access, _item = service()

    with pytest.raises(AgentRunReadError, match="not found") as exc:
        await read.get_run(uuid4())

    assert exc.value.status_code == 404
