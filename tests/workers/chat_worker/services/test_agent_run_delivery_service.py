"""Platform-owned delivery of persisted agent output."""

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
from src.infra.auth.email_service import PlatformEmailSendError
from src.workers.chat_worker.services.agent_run_delivery_service import (
    AgentRunDeliveryExecutionService,
    _canonical_output_markdown,
    _safe_markdown_html,
)


class FakeSession:
    def __init__(self, user=None) -> None:
        self.user = user
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, _statement):
        user = self.user

        class Result:
            def scalar_one_or_none(self):
                return user

        return Result()


def scope(session: FakeSession):
    def factory(*_args):
        async def iterator():
            yield session

        return iterator()

    return factory


class FakeAccess:
    def __init__(self, delivery, run) -> None:
        self.delivery = delivery
        self.run = run
        self.transition: tuple[str, dict] | None = None

    async def claim(self, delivery_id, **_kwargs):
        assert delivery_id == self.delivery.delivery_id
        return self.delivery, self.run

    async def mark_sent(self, _delivery_id, **kwargs):
        self.transition = ("sent", kwargs)
        return True

    async def mark_terminal(self, _delivery_id, **kwargs):
        self.transition = (kwargs["status"], kwargs)
        return True

    async def release_for_retry(self, _delivery_id, **kwargs):
        self.transition = ("retry", kwargs)
        return True


class FakeEmailService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.messages: list[dict] = []
        self.app_name = "CopilotGTM"
        self.app_url = "https://app.copilotgtm.com"

    def _create_styled_html(self, _title, _preheader, content):
        return f"<html>{content}</html>"

    async def send_email_with_receipt(self, **message):
        self.messages.append(message)
        if self.failure:
            raise self.failure
        return "resend-message-1"


def run_and_delivery(*, output_text="**Created lead**", output_json=None):
    organisation_id = uuid4()
    principal_id = uuid4()
    run_id = uuid4()
    delivery = NotificationDelivery(
        organisation_id=organisation_id,
        notification_type="agent_output_ready",
        source_type="agent_run",
        source_id=run_id,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key=str(principal_id),
        status=NotificationDeliveryStatus.SENDING,
        claim_token=uuid4(),
        attempt_count=1,
    )
    run = AgentRun(
        run_id=run_id,
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        execution_principal_type="user",
        execution_principal_id=principal_id,
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
            "name": "Incoming sales email",
            "delivery_config": {"mode": "email_notification"},
        },
        agent_snapshot={"name": "Lead creator"},
        input_snapshot={"subject": {"type": "email_message", "id": str(uuid4())}},
        output_text=output_text,
        output_json=output_json,
        completed_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
    )
    user = SimpleNamespace(
        user_id=principal_id,
        organisation_id=organisation_id,
        email="owner@example.com",
        first_name="Asha",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    return organisation_id, delivery, run, user


@pytest.mark.asyncio
async def test_delivery_emails_full_markdown_from_platform_to_current_user() -> None:
    organisation_id, delivery, run, user = run_and_delivery()
    run.event_key = "crm.deal.created"
    run.input_snapshot = {
        "subject": {"type": "deal", "id": str(uuid4())},
        "record": {"name": "Acme Expansion"},
    }
    org_session = FakeSession()
    access = FakeAccess(delivery, run)
    email = FakeEmailService()
    service = AgentRunDeliveryExecutionService(
        organisation_id,
        session_scope=scope(org_session),
        master_session_scope=scope(FakeSession(user)),
        access_factory=lambda _session, _org: access,
        platform_email_service=email,
    )

    result = await service.execute(
        delivery.delivery_id,
        worker_id="delivery-worker",
        retry_allowed=True,
    )

    assert result["status"] == "sent"
    message = email.messages[0]
    assert message["to_email"] == "owner@example.com"
    assert message["subject"] == "CopilotGTM — Lead creator completed"
    assert message["text_content"].startswith("Hi Asha,\n\n")
    assert "Reason: CRM deal created" in message["text_content"]
    assert "Subject: Deal · Acme Expansion" in message["text_content"]
    assert "View full result:" in message["text_content"]
    assert "<p>Hi Asha,</p>" in message["html_content"]
    assert message["text_content"].endswith("**Created lead**")
    assert "<strong>Created lead</strong>" in message["html_content"]
    assert "Incoming sales email" in message["html_content"]
    assert message["idempotency_key"] == f"agent-output:{delivery.delivery_id}"
    assert access.transition == (
        "sent",
        {
            "claim_token": delivery.claim_token,
            "provider_resource_id": "resend-message-1",
            "sent_at": access.transition[1]["sent_at"],
        },
    )


def test_markdown_rendering_neutralizes_raw_html_and_unsafe_links() -> None:
    rendered = _safe_markdown_html(
        "# Result\n<script>alert(1)</script> [click](javascript:alert(2))"
    )

    assert "<h1>Result</h1>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "javascript:" not in rendered


def test_json_and_oversized_outputs_have_deterministic_markdown() -> None:
    _, _, json_run, _ = run_and_delivery(output_text=None, output_json={"z": 1, "a": 2})
    assert _canonical_output_markdown(json_run, "https://app.example/runs/1") == (
        '```json\n{\n  "a": 2,\n  "z": 1\n}\n```'
    )

    _, _, large_run, _ = run_and_delivery(output_text="x" * 100_001)
    result = _canonical_output_markdown(large_run, "https://app.example/runs/2")
    assert result.startswith("x" * 10_000)
    assert "Output shortened for email" in result
    assert "https://app.example/runs/2" in result

    unicode_output = "🙂" * 60_000
    _, _, unicode_run, _ = run_and_delivery(output_text=unicode_output)
    assert (
        _canonical_output_markdown(
            unicode_run,
            "https://app.example/runs/3",
        )
        == unicode_output
    )


@pytest.mark.asyncio
async def test_inactive_or_missing_recipient_is_skipped_without_sending() -> None:
    organisation_id, delivery, run, _ = run_and_delivery()
    access = FakeAccess(delivery, run)
    email = FakeEmailService()
    service = AgentRunDeliveryExecutionService(
        organisation_id,
        session_scope=scope(FakeSession()),
        master_session_scope=scope(FakeSession(None)),
        access_factory=lambda _session, _org: access,
        platform_email_service=email,
    )

    result = await service.execute(
        delivery.delivery_id,
        worker_id="delivery-worker",
        retry_allowed=True,
    )

    assert result["status"] == "skipped"
    assert email.messages == []
    assert access.transition is not None
    assert access.transition[0] == "SKIPPED"
    assert access.transition[1]["error_code"] == "recipient_unavailable"


@pytest.mark.asyncio
async def test_pre_provider_failure_releases_the_delivery_without_rerunning_agent() -> None:
    organisation_id, delivery, run, user = run_and_delivery()
    access = FakeAccess(delivery, run)
    service = AgentRunDeliveryExecutionService(
        organisation_id,
        session_scope=scope(FakeSession()),
        master_session_scope=scope(FakeSession(user)),
        access_factory=lambda _session, _org: access,
        platform_email_service=FakeEmailService(
            PlatformEmailSendError(
                "platform_email_not_configured",
                "Platform email is not configured",
                safe_to_retry=True,
            )
        ),
    )

    result = await service.execute(
        delivery.delivery_id,
        worker_id="delivery-worker",
        retry_allowed=True,
    )

    assert result["status"] == "retry"
    assert access.transition is not None
    assert access.transition[0] == "retry"


@pytest.mark.asyncio
async def test_unknown_provider_outcome_is_not_automatically_replayed() -> None:
    organisation_id, delivery, run, user = run_and_delivery()
    access = FakeAccess(delivery, run)
    service = AgentRunDeliveryExecutionService(
        organisation_id,
        session_scope=scope(FakeSession()),
        master_session_scope=scope(FakeSession(user)),
        access_factory=lambda _session, _org: access,
        platform_email_service=FakeEmailService(
            PlatformEmailSendError(
                "delivery_outcome_unknown",
                "Provider outcome is unknown",
                safe_to_retry=False,
            )
        ),
    )

    result = await service.execute(
        delivery.delivery_id,
        worker_id="delivery-worker",
        retry_allowed=True,
    )

    assert result["status"] == "needs_attention"
    assert access.transition is not None
    assert access.transition[0] == "NEEDS_ATTENTION"
    assert access.transition[1]["error_code"] == "delivery_outcome_unknown"
