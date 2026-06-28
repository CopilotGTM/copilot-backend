"""Tests for task assignment email recipient selection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.tasks.notification_service import TaskNotificationService


@pytest.mark.asyncio
async def test_assignment_email_uses_internal_assignee_email(monkeypatch) -> None:
    """Internal assignees should receive the same task email template as external assignees."""
    actor_user_id = uuid4()
    sent: list[dict[str, object]] = []

    class _FakeEmailService:
        async def send_task_assignment_email(self, **kwargs):
            """Capture the email payload that would be handed to Resend."""
            sent.append(kwargs)
            return True

    class _FakeMasterSession:
        async def get(self, _model, _key):
            """Return the assigner details normally loaded from public.users."""
            return SimpleNamespace(
                user_id=actor_user_id,
                email="assigner@example.com",
                first_name="Ava",
                last_name="Seller",
            )

    @asynccontextmanager
    async def _master_session_scope():
        yield _FakeMasterSession()

    monkeypatch.setattr(
        "src.service.crm.tasks.notification_service.master_async_session_scope",
        _master_session_scope,
    )
    monkeypatch.setattr(
        "src.infra.auth.email_service.email_service",
        _FakeEmailService(),
    )

    task = SimpleNamespace(
        task_id=uuid4(),
        external_assignee_email=None,
        external_assignee_name=None,
        assigned_to_email="owner@example.com",
        assigned_to_name="Owner User",
        title="Follow up",
        description="Send the deck",
        priority="HIGH",
        due_at=datetime(2026, 6, 24, 15, 0, tzinfo=UTC),
        due_date=None,
        status="NOT_STARTED",
    )
    deal = SimpleNamespace(name="Acme")
    service = TaskNotificationService(
        session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=actor_user_id,
    )

    await service.send_assignment_email(task, deal, is_update=False)

    assert sent == [
        {
            "to_email": "owner@example.com",
            "assignee_name": "Owner User",
            "assigner_name": "Ava Seller",
            "task_subject": "Follow up",
            "task_body": "Send the deck",
            "task_priority": "HIGH",
            "task_due_date": "June 24, 2026 at 03:00 PM",
            "task_status": "NOT_STARTED",
            "deal_name": "Acme",
            "cc_email": "assigner@example.com",
            "is_update": False,
        }
    ]


@pytest.mark.asyncio
async def test_assignment_email_does_not_cc_same_assigner_and_assignee(monkeypatch) -> None:
    """Self-assigned tasks should not duplicate the recipient in cc."""
    actor_user_id = uuid4()
    sent: list[dict[str, object]] = []

    class _FakeEmailService:
        async def send_task_assignment_email(self, **kwargs):
            """Capture the email payload that would be handed to Resend."""
            sent.append(kwargs)
            return True

    class _FakeMasterSession:
        async def get(self, _model, _key):
            """Return an assigner whose email matches the assignee."""
            return SimpleNamespace(
                user_id=actor_user_id,
                email="owner@example.com",
                first_name="Owner",
                last_name="User",
            )

    @asynccontextmanager
    async def _master_session_scope():
        yield _FakeMasterSession()

    monkeypatch.setattr(
        "src.service.crm.tasks.notification_service.master_async_session_scope",
        _master_session_scope,
    )
    monkeypatch.setattr(
        "src.infra.auth.email_service.email_service",
        _FakeEmailService(),
    )

    task = SimpleNamespace(
        task_id=uuid4(),
        external_assignee_email=None,
        external_assignee_name=None,
        assigned_to_email="owner@example.com",
        assigned_to_name="Owner User",
        title="Follow up",
        description=None,
        priority="NONE",
        due_at=None,
        due_date=None,
        status="NOT_STARTED",
    )
    service = TaskNotificationService(
        session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=actor_user_id,
    )

    await service.send_assignment_email(task, None, is_update=False)

    assert sent[0]["to_email"] == "owner@example.com"
    assert sent[0]["cc_email"] is None


@pytest.mark.asyncio
async def test_assignment_notification_sends_whatsapp_to_internal_assignee(monkeypatch) -> None:
    """Internal task assignees with WhatsApp linked should get the template notification too."""
    actor_user_id = uuid4()
    assignee_user_id = uuid4()
    sent_email: list[dict[str, object]] = []
    sent_whatsapp: list[dict[str, object]] = []

    class _FakeEmailService:
        async def send_task_assignment_email(self, **kwargs):
            sent_email.append(kwargs)
            return True

    class _FakeMasterSession:
        async def get(self, _model, key):
            if key == actor_user_id:
                return SimpleNamespace(
                    user_id=actor_user_id,
                    email="assigner@example.com",
                    first_name="Ava",
                    last_name="Seller",
                )
            return None

    @asynccontextmanager
    async def _master_session_scope():
        yield _FakeMasterSession()

    async def _fake_send_task_assignment_whatsapp(**kwargs):
        sent_whatsapp.append(kwargs)
        return True

    monkeypatch.setattr(
        "src.service.crm.tasks.notification_service.master_async_session_scope",
        _master_session_scope,
    )
    monkeypatch.setattr(
        "src.infra.auth.email_service.email_service",
        _FakeEmailService(),
    )
    monkeypatch.setattr(
        "src.service.crm.tasks.notification_service.send_task_assignment_whatsapp",
        _fake_send_task_assignment_whatsapp,
    )

    task = SimpleNamespace(
        task_id=uuid4(),
        external_assignee_email=None,
        external_assignee_name=None,
        assigned_to_user_id=assignee_user_id,
        assigned_to_email="owner@example.com",
        assigned_to_name="Owner User",
        title="Follow up",
        description="Send the deck",
        priority="HIGH",
        due_at=datetime(2026, 6, 24, 15, 0, tzinfo=UTC),
        due_date=None,
        status="NOT_STARTED",
    )
    deal = SimpleNamespace(deal_id=uuid4(), name="Acme")
    service = TaskNotificationService(
        session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=actor_user_id,
    )

    await service.send_assignment_email(task, deal, is_update=False)

    assert sent_email[0]["to_email"] == "owner@example.com"
    assert sent_whatsapp == [
        {
            "organisation_id": service.organisation_id,
            "user_id": assignee_user_id,
            "task": task,
            "deal": deal,
            "assigner_name": "Ava Seller",
            "is_update": False,
            "due_date_text": "June 24, 2026 at 03:00 PM",
        }
    ]
