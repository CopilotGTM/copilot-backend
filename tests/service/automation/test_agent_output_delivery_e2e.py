"""Cross-service proofs for agent output notification semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.domain.models.organisation_tables.sync import (
    NotificationDelivery,
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
)
from src.service.automation.runs.read_service import _delivery_summary
from src.workers.chat_worker.services.agent_run_delivery_service import (
    AgentRunDeliveryExecutionService,
)


class Session:
    def __init__(self, user=None) -> None:
        self.user = user

    async def commit(self) -> None:
        return None

    async def execute(self, _statement):
        user = self.user

        class Result:
            def scalar_one_or_none(self):
                return user

        return Result()


def scope(session):
    def factory(*_args):
        async def iterator():
            yield session

        return iterator()

    return factory


class ClaimOnceAccess:
    def __init__(self, delivery, run) -> None:
        self.delivery = delivery
        self.run = run
        self.claimed = False

    async def claim(self, *_args, **_kwargs):
        if self.claimed:
            return None
        self.claimed = True
        return self.delivery, self.run

    async def mark_sent(self, *_args, **_kwargs):
        self.delivery.status = NotificationDeliveryStatus.SENT
        return True


class PlatformEmail:
    app_name = "CopilotGTM"
    app_url = "https://app.copilotgtm.com"

    def __init__(self) -> None:
        self.calls = 0

    def _create_styled_html(self, _title, _preheader, content):
        return content

    async def send_email_with_receipt(self, **_message):
        self.calls += 1
        return "provider-message-1"


@pytest.mark.asyncio
async def test_replayed_worker_message_does_not_send_persisted_output_twice() -> None:
    organisation_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(
        run_id=run_id,
        organisation_id=organisation_id,
        created_by_user_id=user_id,
        execution_principal_type="user",
        execution_principal_id=user_id,
        audience_mode="principal",
        audience_config={},
        agent_id=uuid4(),
        trigger_id=uuid4(),
        agent_revision=1,
        status=AgentRunStatus.SUCCEEDED,
        idempotency_key=f"run:{run_id}",
        event_id=uuid4(),
        event_key="email.message.received",
        correlation_id=str(uuid4()),
        trigger_snapshot={
            "name": "Incoming email",
            "delivery_config": {"mode": "email_notification"},
        },
        agent_snapshot={"name": "Lead creator"},
        input_snapshot={},
        output_text="Created the lead and linked the account.",
        completed_at=datetime.now(UTC),
    )
    delivery = NotificationDelivery(
        organisation_id=organisation_id,
        notification_type="agent_output_ready",
        source_type="agent_run",
        source_id=run_id,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key=str(user_id),
        status=NotificationDeliveryStatus.SENDING,
        claim_token=uuid4(),
        attempt_count=1,
    )
    user = SimpleNamespace(
        user_id=user_id,
        organisation_id=organisation_id,
        email="owner@example.com",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    access = ClaimOnceAccess(delivery, run)
    email = PlatformEmail()
    service = AgentRunDeliveryExecutionService(
        organisation_id,
        session_scope=scope(Session()),
        master_session_scope=scope(Session(user)),
        access_factory=lambda *_args: access,
        platform_email_service=email,
    )

    first = await service.execute(delivery.delivery_id, worker_id="worker", retry_allowed=True)
    second = await service.execute(delivery.delivery_id, worker_id="worker", retry_allowed=True)

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert email.calls == 1


def test_organisation_fanout_reports_partial_success_without_changing_run_status() -> None:
    organisation_id, run_id = uuid4(), uuid4()
    deliveries = [
        NotificationDelivery(
            organisation_id=organisation_id,
            notification_type="agent_output_ready",
            source_type="agent_run",
            source_id=run_id,
            channel=NotificationDeliveryChannel.EMAIL,
            target_key=str(uuid4()),
            status=status,
        )
        for status in (
            NotificationDeliveryStatus.SENT,
            NotificationDeliveryStatus.SENT,
            NotificationDeliveryStatus.FAILED,
        )
    ]

    summary = _delivery_summary(deliveries)

    assert summary is not None
    assert summary.status == "partially_sent"
    assert summary.recipient_count == 3
    assert summary.sent_count == 2
    assert summary.failed_count == 1
