"""Audience-scoped run-history and notification aggregation contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.domain.models.organisation_tables.sync import (
    NotificationDelivery,
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
)
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.runs.read_service import AgentRunReadError, AgentRunReadService


def run(*, organisation_id: UUID, principal_user_id: UUID) -> AgentRun:
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
        trigger_snapshot={
            "trigger_id": str(trigger_id),
            "task_config": {},
            "delivery_config": {"mode": "email_notification"},
        },
        input_snapshot=event.model_dump(mode="json"),
        output_text="Call buyer",
        attempt_count=1,
        created_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )


def delivery(item: AgentRun, status: NotificationDeliveryStatus, **values) -> NotificationDelivery:
    return NotificationDelivery(
        organisation_id=item.organisation_id,
        notification_type="agent_output_ready",
        source_type="agent_run",
        source_id=item.run_id,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key=str(values.pop("target_key", uuid4())),
        status=status,
        **values,
    )


class FakeAccess:
    def __init__(self) -> None:
        self.rows: list[AgentRun] = []
        self.item: AgentRun | None = None
        self.deliveries: dict[UUID, list[NotificationDelivery]] = {}
        self.list_call = None
        self.requeued: list[tuple[UUID, UUID, str]] = []

    async def list_runs(self, **kwargs):
        self.list_call = kwargs
        return self.rows

    async def get_run(self, _run_id):
        return self.item

    async def list_deliveries(self, run_ids):
        return {run_id: self.deliveries.get(run_id, []) for run_id in run_ids}

    async def get_deliveries(self, run_id):
        return self.deliveries.get(run_id, [])

    async def requeue_repairable_delivery(self, run_id, delivery_id, *, expected_error_code):
        self.requeued.append((run_id, delivery_id, expected_error_code))
        return delivery_id


def service():
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
    assert page.items[0].subject_type == "deal"
    assert page.items[0].output_preview == "Call buyer"
    assert page.next_cursor is None
    assert access.list_call["limit"] == 21


@pytest.mark.asyncio
async def test_list_keeps_success_and_partial_notification_state_separate() -> None:
    read, access, item = service()
    access.rows = [item]
    access.deliveries[item.run_id] = [
        delivery(item, NotificationDeliveryStatus.SENT, sent_at=datetime.now(UTC)) for _ in range(3)
    ] + [
        delivery(
            item,
            NotificationDeliveryStatus.FAILED,
            error_code="provider_rejected",
            last_error="Provider rejected the message",
        )
    ]

    summary = (
        await read.list_runs(
            agent_id=item.agent_id,
            trigger_id=None,
            status=None,
            cursor=None,
            limit=20,
        )
    ).items[0]

    assert summary.status == AgentRunStatus.SUCCEEDED
    assert summary.delivery is not None
    assert summary.delivery.status == "partially_sent"
    assert summary.delivery.recipient_count == 4
    assert summary.delivery.sent_count == 3
    assert summary.delivery.failed_count == 1


@pytest.mark.asyncio
async def test_list_keeps_mixed_pending_and_failed_targets_in_progress() -> None:
    read, access, item = service()
    access.rows = [item]
    access.deliveries[item.run_id] = [
        delivery(item, NotificationDeliveryStatus.PENDING),
        delivery(
            item,
            NotificationDeliveryStatus.FAILED,
            error_code="provider_rejected",
            last_error="Provider rejected the message",
        ),
    ]

    summary = (
        await read.list_runs(
            agent_id=item.agent_id,
            trigger_id=None,
            status=None,
            cursor=None,
            limit=20,
        )
    ).items[0]

    assert summary.delivery is not None
    assert summary.delivery.status == "pending"
    assert summary.delivery.failed_count == 1


@pytest.mark.asyncio
async def test_repaired_pre_provider_target_can_be_requeued_without_rerunning_agent() -> None:
    read, access, item = service()
    access.item = item
    target = delivery(
        item,
        NotificationDeliveryStatus.NEEDS_ATTENTION,
        error_code="platform_email_not_configured",
        last_error="Platform email is not configured",
    )
    access.deliveries[item.run_id] = [target]

    result, delivery_ids = await read.retry_delivery(item.run_id, target.delivery_id)

    assert result.run_id == item.run_id
    assert result.status == "pending"
    assert result.delivery_count == 1
    assert delivery_ids == (target.delivery_id,)
    assert access.requeued == [(item.run_id, target.delivery_id, "platform_email_not_configured")]


@pytest.mark.asyncio
async def test_unknown_provider_outcome_cannot_be_requeued() -> None:
    read, access, item = service()
    access.item = item
    target = delivery(
        item,
        NotificationDeliveryStatus.NEEDS_ATTENTION,
        error_code="delivery_outcome_unknown",
        last_error="Provider outcome is unknown",
    )
    access.deliveries[item.run_id] = [target]

    with pytest.raises(AgentRunReadError, match="cannot be retried safely") as exc:
        await read.retry_delivery(item.run_id, target.delivery_id)

    assert exc.value.status_code == 409
    assert access.requeued == []


@pytest.mark.asyncio
async def test_detail_returns_snapshots_and_delivery_without_provider_metadata() -> None:
    read, access, item = service()
    access.item = item
    target = delivery(
        item,
        NotificationDeliveryStatus.SENT,
        provider_resource_id="provider-message-id",
        sent_at=datetime(2026, 7, 14, 9, 1, tzinfo=UTC),
    )
    access.deliveries[item.run_id] = [target]

    detail = await read.get_run(item.run_id)

    assert detail.source_domain == "crm"
    assert detail.agent_snapshot == {"prompt": "Coach deal"}
    assert detail.delivery is not None
    assert detail.delivery.status == "sent"
    assert "provider_resource_id" not in detail.delivery.model_dump_json()


@pytest.mark.asyncio
async def test_detail_hides_foreign_or_missing_run_as_not_found() -> None:
    read, _access, _item = service()

    with pytest.raises(AgentRunReadError, match="not found") as exc:
        await read.get_run(uuid4())

    assert exc.value.status_code == 404
