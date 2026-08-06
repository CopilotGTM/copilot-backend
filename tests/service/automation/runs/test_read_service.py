"""Audience-scoped run-history service contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.runs.read_service import AgentRunReadError, AgentRunReadService


def run(*, organisation_id: UUID, principal_user_id: UUID) -> AgentRun:
    """Build the run fixture used by the surrounding regression cases."""
    event_id, trigger_id, subject_id = uuid4(), uuid4(), uuid4()
    event = AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(organisation_id),
            "event_key": "crm.deal.created",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 7, 14, 8, 55, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "create",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(principal_user_id)},
            "subject": {"type": "deal", "id": str(subject_id)},
            "record": {"id": str(subject_id), "owner_id": str(principal_user_id)},
            "correlation_id": "request-123",
            "idempotency_key": f"crm.deal.created:{event_id}",
        }
    )
    return AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=principal_user_id,
        execution_principal_type="user",
        execution_principal_id=principal_user_id,
        audience_mode="principal",
        audience_config={},
        agent_id=uuid4(),
        trigger_id=trigger_id,
        agent_revision=2,
        status=AgentRunStatus.SUCCEEDED,
        idempotency_key=f"trigger:{trigger_id}:event:{event_id}",
        event_id=event_id,
        event_key=event.event_key,
        correlation_id="request-123",
        agent_snapshot={"prompt": "Coach deal"},
        trigger_snapshot={"trigger_id": str(trigger_id), "task_config": {}},
        input_snapshot=event.model_dump(mode="json"),
        output_text="Call buyer",
        attempt_count=1,
        created_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.rows = []
        self.item = None
        self.list_call = None

    async def list_runs(self, **kwargs):
        """Expose the list runs operation expected by the Fake Access test double."""
        self.list_call = kwargs
        return self.rows

    async def get_run(self, _run_id):
        """Expose the get run operation expected by the Fake Access test double."""
        return self.item


def service():
    """Build the service fixture used by the surrounding regression cases."""
    organisation_id = uuid4()
    principal_user_id = uuid4()
    item = run(organisation_id=organisation_id, principal_user_id=principal_user_id)
    access = FakeAccess()
    result = AgentRunReadService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        principal_user_id,
        access=access,
    )
    return result, access, item


@pytest.mark.asyncio
async def test_list_returns_canonical_event_metadata_and_cursor_page() -> None:
    """Verify the contract that list returns canonical event metadata and cursor page."""
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
    assert page.items[0].event_key == "crm.deal.created"
    assert page.items[0].subject_type == "deal"
    assert page.items[0].output_preview == "Call buyer"
    assert page.next_cursor is None
    assert access.list_call["limit"] == 21


@pytest.mark.asyncio
async def test_detail_returns_causation_source_snapshots_and_usage_metadata() -> None:
    """Verify the contract that detail returns causation source snapshots and usage metadata."""
    read, access, item = service()
    access.item = item

    detail = await read.get_run(item.run_id)

    assert detail.source_domain == "crm"
    assert detail.source_operation == "create"
    assert detail.correlation_id == "request-123"
    assert detail.agent_snapshot == {"prompt": "Coach deal"}
    assert detail.input_snapshot["subject"]["type"] == "deal"


@pytest.mark.asyncio
async def test_detail_hides_foreign_or_missing_run_as_not_found() -> None:
    """Verify the contract that detail hides foreign or missing run as not found."""
    read, _access, _item = service()

    with pytest.raises(AgentRunReadError, match="not found") as exc:
        await read.get_run(uuid4())

    assert exc.value.status_code == 404
