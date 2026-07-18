"""Tests for the agent send_email tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from src.agents.tools.communication.send_email import create_send_email, send_email
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


class _UserResult:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self._users = users

    def scalars(self) -> _UserResult:
        return self

    def first(self) -> SimpleNamespace | None:
        return self._users[0] if self._users else None

    def all(self) -> list[SimpleNamespace]:
        return self._users


class _UserSession:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self.users = users

    async def execute(self, stmt: object) -> _UserResult:
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        if "@" in compiled:
            return _UserResult([user for user in self.users if user.email.lower() in compiled])
        if "concat" not in compiled:
            return _UserResult([])
        return _UserResult(self.users)


def _user_scope(users: list[SimpleNamespace]):
    async def scope():
        yield _UserSession(users)

    return scope()


def _user(first_name: str, last_name: str, email: str) -> SimpleNamespace:
    return SimpleNamespace(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        first_name=first_name,
        last_name=last_name,
        email=email,
    )


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
async def test_send_email_requires_confirmation_for_exact_explicit_payload(monkeypatch) -> None:
    """Even a fully resolved direct-send payload remains read-only until confirmation."""

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
        sender_email="seller@example.com",
    )

    assert result["status"] == "confirmation_required"
    assert "final_send_confirmation" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_requires_exact_recipient_email_for_names(monkeypatch) -> None:
    """An unresolved display name cannot become an external send recipient."""

    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _user_scope([]),
    )

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
async def test_send_email_resolves_internal_user_name_and_requires_confirmation(
    monkeypatch,
) -> None:
    service = MagicMock()
    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", service)
    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _user_scope([_user("Nikhil", "Singhal", "nikhil@copilotgtm.com")]),
    )

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=["Nikhil Singhal"],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert "recipient_resolved_from_name" in result["confirmation_reasons"]
    assert result["confirmation_payload"]["to"] == [
        {"email": "nikhil@copilotgtm.com", "name": "Nikhil Singhal"}
    ]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_sends_after_confirming_resolved_internal_user_name(
    monkeypatch,
) -> None:
    created_requests = []

    class FakeService:
        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def send_email(self, request):
            created_requests.append(request)
            return EmailSendCreateResult(
                request_id=UUID("33333333-3333-3333-3333-333333333333"),
                status="sent",
                subject=request.subject,
                provider_status_code=202,
            )

    monkeypatch.setattr("src.agents.tools.communication.send_email.EmailSendService", FakeService)
    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _user_scope([_user("Nikhil", "Singhal", "nikhil@copilotgtm.com")]),
    )

    result = await send_email(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        to=["Nikhil Singhal"],
        cc=["buyer@example.com"],
        bcc=["Nikhil Singhal"],
        subject="Follow-up",
        body="Thanks.",
        sender_email="seller@contoso.com",
        confirmed=True,
    )

    assert result["status"] == "sent"
    assert created_requests[0].to[0].email == "nikhil@copilotgtm.com"
    assert created_requests[0].to[0].name == "Nikhil Singhal"
    assert created_requests[0].cc[0].email == "buyer@example.com"
    assert created_requests[0].bcc[0].email == "nikhil@copilotgtm.com"


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


def test_send_email_tool_description_allows_gmail_and_outlook() -> None:
    tool = create_send_email(
        _FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        ),
        lambda: None,
    )

    assert "Gmail or Outlook" in (tool.__doc__ or "")
    assert "not replies" in (tool.__doc__ or "")
