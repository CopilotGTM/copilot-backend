"""Tests for meeting summary ready notification delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


def test_email_rendering_links_to_app_without_embedding_summary_content(monkeypatch):
    """Email notifications should announce availability, not leak generated summary text."""
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
        NotificationRecipient,
    )

    org_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_notification_service.app_config.APP_URL",
        "https://app.example.test",
    )
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=deal_id,
        deal_name="Big Deal",
        event_id=event_id,
        meeting_title="Pricing Review",
        meeting_start_at=datetime(2026, 6, 9, 12, 30, tzinfo=UTC),
        summary_data={
            "data": {
                "executive_summary": "Customer asked for a confidential discount.",
            }
        },
    )
    recipient = NotificationRecipient(
        user_id=uuid4(),
        email="seller@example.com",
        first_name="Seller",
    )

    rendered = service.render_email(context, recipient)

    assert rendered.subject == "Meeting summary is ready: Pricing Review"
    assert f"/deals/{deal_id}/briefing-room?meeting={event_id}&amp;summary=1" in rendered.html
    assert f"/deals/{deal_id}/briefing-room?meeting={event_id}&summary=1" in rendered.text
    assert "Customer asked for a confidential discount" not in rendered.html
    assert "Customer asked for a confidential discount" not in rendered.text


def test_channel_rendering_links_to_app_without_embedding_summary_content(monkeypatch):
    """Channel notifications may include the link, but not the full summary."""
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
    )

    org_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_notification_service.app_config.APP_URL",
        "https://app.example.test/",
    )
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=deal_id,
        deal_name="Big Deal",
        event_id=event_id,
        meeting_title=None,
        meeting_start_at=None,
        summary_data={"data": {"summary": "Do not post this generated summary to channels."}},
    )

    text = service.render_channel_text(context)

    assert "Meeting summary is ready" in text
    assert (
        f"https://app.example.test/deals/{deal_id}/briefing-room?meeting={event_id}&summary=1"
        in text
    )
    assert "Do not post this generated summary to channels" not in text


@pytest.mark.asyncio
async def test_resolve_email_recipients_limits_to_internal_meeting_attendees(monkeypatch):
    """Only active approved org users on the meeting invite should receive email."""
    from src.domain.models.master_tables.super_admin_models import AdminUserStatus
    from src.domain.models.master_tables.user_models import SupabaseUserStatus
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationService,
    )

    org_id = uuid4()
    event_id = uuid4()
    active_attendee = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=org_id,
        email="seller@example.com",
        first_name="Active",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    active_non_attendee = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=org_id,
        email="observer@example.com",
        first_name="Observer",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    inactive_user = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=org_id,
        email="inactive-attendee@example.com",
        first_name="Inactive",
        status=SupabaseUserStatus.INACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    pending_user = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=org_id,
        email="pending@example.com",
        first_name="Pending",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.PENDING,
    )
    cross_org_user = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=uuid4(),
        email="client@example.com",
        first_name="Client",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    service = MeetingSummaryNotificationService(org_id)

    monkeypatch.setattr(
        service,
        "_fetch_org_users",
        lambda: [active_attendee, active_non_attendee, inactive_user, pending_user, cross_org_user],
    )
    monkeypatch.setattr(
        service,
        "_fetch_internal_recipient_emails_for_event",
        lambda _event_id: {
            "seller@example.com",
            "inactive-attendee@example.com",
            "client@example.com",
        },
    )

    recipients = await service.resolve_email_recipients(event_id=event_id)

    assert [recipient.email for recipient in recipients] == ["seller@example.com"]


@pytest.mark.asyncio
async def test_resolve_email_recipients_without_attendee_match_returns_no_recipients(monkeypatch):
    """A completed summary should not fan out to the whole tenant when no internal attendee matches."""
    from src.domain.models.master_tables.super_admin_models import AdminUserStatus
    from src.domain.models.master_tables.user_models import SupabaseUserStatus
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationService,
    )

    org_id = uuid4()
    service = MeetingSummaryNotificationService(org_id)
    monkeypatch.setattr(
        service,
        "_fetch_org_users",
        lambda: [
            SimpleNamespace(
                user_id=uuid4(),
                organisation_id=org_id,
                email="observer@example.com",
                first_name="Observer",
                status=SupabaseUserStatus.ACTIVE,
                admin_status=AdminUserStatus.APPROVED,
            )
        ],
    )
    monkeypatch.setattr(
        service,
        "_fetch_internal_recipient_emails_for_event",
        lambda _event_id: {"external@example.com"},
    )

    assert await service.resolve_email_recipients(event_id=uuid4()) == []


@pytest.mark.asyncio
async def test_resolve_channel_targets_returns_only_active_deal_linked_channels(monkeypatch):
    """Channel fanout should be scoped to active channels linked to the summary's deal."""
    from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationService,
    )

    org_id = uuid4()
    deal_id = uuid4()
    slack_channel_id = uuid4()
    teams_channel_id = uuid4()
    service = MeetingSummaryNotificationService(org_id)

    monkeypatch.setattr(
        service,
        "_fetch_deal_linked_channel_rows",
        lambda _deal_id: [
            SimpleNamespace(
                channel_id=slack_channel_id,
                external_channel_id="C123",
                channel_name="deal-room",
                integration_type=IntegrationType.SLACK,
                channel_metadata={"workspace_id": "T123"},
                is_archived="false",
                is_deleted=False,
            ),
            SimpleNamespace(
                channel_id=teams_channel_id,
                external_channel_id="19:chat",
                channel_name="teams-room",
                integration_type=IntegrationType.TEAMS,
                channel_metadata={
                    "service_url": "https://smba.trafficmanager.net/amer/",
                    "conversation_id": "19:chat",
                },
                is_archived="false",
                is_deleted=False,
            ),
            SimpleNamespace(
                channel_id=uuid4(),
                external_channel_id="archived",
                channel_name="old-room",
                integration_type=IntegrationType.SLACK,
                channel_metadata={"workspace_id": "T999"},
                is_archived="true",
                is_deleted=False,
            ),
        ],
    )

    targets = await service.resolve_channel_targets(deal_id)

    assert [(target.provider, target.channel_id) for target in targets] == [
        ("slack", slack_channel_id),
        ("teams", teams_channel_id),
    ]
    assert targets[0].delivery_context == {"team_id": "T123", "channel_id": "C123"}
    assert targets[1].delivery_context == {
        "service_url": "https://smba.trafficmanager.net/amer/",
        "conversation_id": "19:chat",
    }


@pytest.mark.asyncio
async def test_teams_channel_without_proactive_context_is_skipped(monkeypatch):
    """Teams channels without Bot Framework proactive context are not sendable."""
    from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationService,
    )

    service = MeetingSummaryNotificationService(uuid4())
    monkeypatch.setattr(
        service,
        "_fetch_deal_linked_channel_rows",
        lambda _deal_id: [
            SimpleNamespace(
                channel_id=uuid4(),
                external_channel_id="19:teams-chat",
                channel_name="teams-room",
                integration_type=IntegrationType.TEAMS,
                channel_metadata={"team_id": "team-1"},
                is_archived="false",
                is_deleted=False,
            )
        ],
    )

    assert await service.resolve_channel_targets(uuid4()) == []


@pytest.mark.asyncio
async def test_claim_delivery_uses_postgres_unique_constraint(monkeypatch):
    """The generic delivery claim must use the table-level idempotency constraint."""
    from sqlalchemy.dialects import postgresql
    from src.domain.models.organisation_tables.sync import NotificationDeliveryChannel
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
    )

    class _InsertResult:
        def scalar_one_or_none(self):
            return uuid4()

    class _CapturingSession:
        def __init__(self):
            self.sql = ""

        async def execute(self, stmt):
            self.sql = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            return _InsertResult()

        async def commit(self):
            return None

    async def _yield_session(session):
        yield session

    org_id = uuid4()
    session = _CapturingSession()
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_notification_service.acquire_org_async_session",
        lambda _org_id: _yield_session(session),
    )
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=uuid4(),
        deal_name="Deal",
        event_id=uuid4(),
        meeting_title="Meeting",
        meeting_start_at=None,
        summary_data={"summary": "ready"},
        association_id=uuid4(),
    )

    delivery_id = await service._claim_delivery(  # noqa: SLF001
        context=context,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key="user-1",
        target_label="seller@example.com",
    )

    assert delivery_id is not None
    assert "ON CONFLICT ON CONSTRAINT uq_notification_delivery_target DO NOTHING" in session.sql


@pytest.mark.asyncio
async def test_claim_delivery_retries_existing_failed_delivery(monkeypatch):
    """A failed delivery row should be claimed again instead of creating a duplicate target."""
    from src.domain.models.organisation_tables.sync import (
        NotificationDeliveryChannel,
        NotificationDeliveryStatus,
    )
    from src.service.meeting_bot.summary_notification_service import (
        CALENDAR_EVENT_DEAL_ASSOCIATION_SOURCE,
        MEETING_SUMMARY_READY_NOTIFICATION,
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
    )

    delivery_id = uuid4()
    org_id = uuid4()
    association_id = uuid4()
    existing = SimpleNamespace(
        delivery_id=delivery_id,
        status=NotificationDeliveryStatus.FAILED,
        attempt_count=1,
        last_error="temporary failure",
        updated_at=None,
        delivery_metadata={},
    )

    class _InsertResult:
        def scalar_one_or_none(self):
            return None

    class _SelectScalars:
        def first(self):
            return existing

    class _SelectResult:
        def scalars(self):
            return _SelectScalars()

    class _RetrySession:
        def __init__(self):
            self.execute_count = 0
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, _stmt):
            self.execute_count += 1
            return _InsertResult() if self.execute_count == 1 else _SelectResult()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    async def _yield_session(session):
        yield session

    session = _RetrySession()
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_notification_service.acquire_org_async_session",
        lambda _org_id: _yield_session(session),
    )
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=uuid4(),
        deal_name="Deal",
        event_id=uuid4(),
        meeting_title="Meeting",
        meeting_start_at=None,
        summary_data={"summary": "ready"},
        association_id=association_id,
    )

    claimed = await service._claim_delivery(  # noqa: SLF001
        context=context,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key="user-1",
        target_label="seller@example.com",
        metadata={"email": "seller@example.com"},
    )

    assert claimed == delivery_id
    assert existing.status == NotificationDeliveryStatus.SENDING
    assert existing.attempt_count == 2
    assert existing.last_error is None
    assert existing.delivery_metadata == {
        "deal_id": str(context.deal_id),
        "email": "seller@example.com",
    }
    assert session.commits == 1
    assert session.rollbacks == 0
    assert MEETING_SUMMARY_READY_NOTIFICATION == "meeting_summary_ready"
    assert CALENDAR_EVENT_DEAL_ASSOCIATION_SOURCE == "calendar_event_deal_association"


@pytest.mark.asyncio
async def test_claim_delivery_does_not_reclaim_in_flight_delivery(monkeypatch):
    """An active SENDING row belongs to another worker and must not be sent twice."""
    from src.domain.models.organisation_tables.sync import (
        NotificationDeliveryChannel,
        NotificationDeliveryStatus,
    )
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
    )

    existing = SimpleNamespace(
        delivery_id=uuid4(),
        status=NotificationDeliveryStatus.SENDING,
        attempt_count=1,
        last_error=None,
        updated_at=datetime.now(UTC),
        delivery_metadata={},
    )

    class _InsertResult:
        def scalar_one_or_none(self):
            return None

    class _SelectScalars:
        def first(self):
            return existing

    class _SelectResult:
        def scalars(self):
            return _SelectScalars()

    class _InFlightSession:
        def __init__(self):
            self.execute_count = 0
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, _stmt):
            self.execute_count += 1
            return _InsertResult() if self.execute_count == 1 else _SelectResult()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    async def _yield_session(session):
        yield session

    org_id = uuid4()
    session = _InFlightSession()
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_notification_service.acquire_org_async_session",
        lambda _org_id: _yield_session(session),
    )
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=uuid4(),
        deal_name="Deal",
        event_id=uuid4(),
        meeting_title="Meeting",
        meeting_start_at=None,
        summary_data={"summary": "ready"},
        association_id=uuid4(),
    )

    claimed = await service._claim_delivery(  # noqa: SLF001
        context=context,
        channel=NotificationDeliveryChannel.EMAIL,
        target_key="user-1",
        target_label="seller@example.com",
    )

    assert claimed is None
    assert existing.status == NotificationDeliveryStatus.SENDING
    assert existing.attempt_count == 1
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_notify_reports_failed_when_email_provider_fails(monkeypatch):
    """Per-target failures must bubble to the worker so Celery can retry them."""
    from src.service.meeting_bot.summary_notification_service import (
        MeetingSummaryNotificationContext,
        MeetingSummaryNotificationService,
        NotificationRecipient,
    )

    org_id = uuid4()
    service = MeetingSummaryNotificationService(org_id)
    context = MeetingSummaryNotificationContext(
        organisation_id=org_id,
        organisation_name="Acme",
        deal_id=uuid4(),
        deal_name="Deal",
        event_id=uuid4(),
        meeting_title="Meeting",
        meeting_start_at=None,
        summary_data={"summary": "ready"},
        association_id=uuid4(),
    )
    recipient = NotificationRecipient(
        user_id=uuid4(),
        email="seller@example.com",
        first_name="Seller",
    )
    claimed_delivery_id = uuid4()
    marked_failed: list[tuple[object, str]] = []

    async def _load_context(**_kwargs):
        return context

    async def _resolve_email_recipients(**_kwargs):
        return [recipient]

    async def _resolve_channel_targets(_deal_id):
        return []

    async def _claim_delivery(**_kwargs):
        return claimed_delivery_id

    async def _send_email(*_args):
        return False

    monkeypatch.setattr(service, "load_context", _load_context)
    monkeypatch.setattr(service, "resolve_email_recipients", _resolve_email_recipients)
    monkeypatch.setattr(service, "resolve_channel_targets", _resolve_channel_targets)
    monkeypatch.setattr(service, "_claim_delivery", _claim_delivery)
    monkeypatch.setattr(service, "send_email", _send_email)

    async def _mark_failed(delivery_id, error):
        marked_failed.append((delivery_id, error))

    monkeypatch.setattr(service, "_mark_delivery_failed", _mark_failed)

    result = await service.notify(deal_id=context.deal_id, event_id=context.event_id)

    assert result == {
        "status": "failed",
        "targets_found": 1,
        "sent": 0,
        "skipped": 0,
        "failed": 1,
    }
    assert marked_failed == [(claimed_delivery_id, "email_send_failed")]
