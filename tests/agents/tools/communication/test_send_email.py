"""Tests for the agent send_email tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from src.agents.tools.communication.send_email import send_email
from src.service.email_sends.models import EmailSendCreateResult


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    channel: str = "web"
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        yield MagicMock()


@pytest.mark.asyncio
async def test_send_email_requires_current_user() -> None:
    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=None,
        ),
        to=["buyer@example.com"],
        subject="Follow-up",
        body="Thanks.",
    )

    assert result["status"] == "error"
    assert "user" in result["message"].lower()


@pytest.mark.asyncio
async def test_send_email_requires_required_fields() -> None:
    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=[],
        subject="",
        body="",
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "email_send_missing_required_fields"


@pytest.mark.asyncio
async def test_send_email_requires_confirmation_when_sender_missing(monkeypatch) -> None:
    service = MagicMock()
    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", service)

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=["buyer@example.com"],
        subject="Follow-up",
        body="Thanks.",
    )

    assert result["status"] == "confirmation_required"
    assert result["code"] == "email_send_confirmation_required"
    assert "sender_not_explicit" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_requires_exact_recipient_email_for_names() -> None:
    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=["Saksham Bhutani"],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
        confirmed=True,
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "email_send_recipient_email_required"


@pytest.mark.asyncio
async def test_send_email_requires_confirmation_for_resolved_recipient(monkeypatch) -> None:
    service = MagicMock()
    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", service)

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=[
            {
                "email": "saksham@example.com",
                "name": "Saksham Bhutani",
                "source": "contact",
            }
        ],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert "recipient_resolved_from_name" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_requires_confirmation_for_named_recipient_dict(monkeypatch) -> None:
    service = MagicMock()
    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", service)

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=[
            {
                "email": "saksham@example.com",
                "name": "Saksham Bhutani",
            }
        ],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert "recipient_resolved_from_name" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_calls_service_for_confirmed_explicit_email(monkeypatch) -> None:
    created_requests = []

    class FakeService:
        def __init__(self, session, *, organisation_id, requested_by_user_id):
            assert organisation_id == UUID("11111111-1111-1111-1111-111111111111")
            assert requested_by_user_id == UUID("22222222-2222-2222-2222-222222222222")

        async def send_email(self, request):
            created_requests.append(request)
            return EmailSendCreateResult(
                request_id=UUID("33333333-3333-3333-3333-333333333333"),
                status="sent",
                subject=request.subject,
                provider_status_code=202,
            )

    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", FakeService)

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=["buyer@example.com"],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
        confirmed=True,
    )

    assert result["status"] == "sent"
    assert result["request_id"] == "33333333-3333-3333-3333-333333333333"
    assert result["activity_request_id"] == "33333333-3333-3333-3333-333333333333"
    assert result["activity_type"] == "send_email"
    assert result["linked_record_type"] is None
    assert result["linked_record_id"] is None
    assert result["provider_resource_id"] is None
    assert result["provider_status_code"] == 202
    assert created_requests[0].to[0].email == "buyer@example.com"
    assert created_requests[0].sender_email == "seller@contoso.com"
