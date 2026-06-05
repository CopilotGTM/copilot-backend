"""Tests for Outlook email send orchestration and audit persistence."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import (
    IntegrationActivityRequestStatus,
    IntegrationType,
)
from src.service.email_sends.models import EmailSendAddress, EmailSendCreateRequest, EmailSendError
from src.service.email_sends.service import EmailSendService, derive_email_idempotency_key


class _FakeSession:
    def __init__(self, existing_row=None) -> None:
        self.added: list[object] = []
        self.committed = 0
        self.flushed = 0
        self.existing_row = existing_row

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        pass

    async def execute(self, statement):
        row = self.existing_row

        class _Scalars:
            def first(self):
                return row

            def scalar_one_or_none(self):
                return None

        class _Result:
            def scalars(self):
                return _Scalars()

            def scalar_one_or_none(self):
                return row

        return _Result()


class _FakeResolver:
    def __init__(self, integration_type=IntegrationType.MICROSOFT) -> None:
        self.integration_type = integration_type

    async def resolve(self, session, organisation_id, requested_by_user_id, *, sender_email=None):
        return SimpleNamespace(
            integration=SimpleNamespace(
                integration_id=UUID("33333333-3333-3333-3333-333333333333"),
                integration_email=sender_email or "seller@contoso.com",
                integration_type=self.integration_type,
            ),
            token_info=SimpleNamespace(access_token="token"),
        )


class _FakeEmailClient:
    def __init__(
        self,
        session: _FakeSession | None = None,
        *,
        provider_status_code: int = 202,
        provider_resource_id: str | None = None,
    ) -> None:
        self.requests: list[EmailSendCreateRequest] = []
        self.session = session
        self.provider_status_code = provider_status_code
        self.provider_resource_id = provider_resource_id

    async def send_mail(self, token_info, request: EmailSendCreateRequest):
        if self.session is not None:
            assert self.session.committed == 1
        self.requests.append(request)
        return SimpleNamespace(
            provider_status_code=self.provider_status_code,
            provider_resource_id=self.provider_resource_id,
        )


class _FakeGraphClient(_FakeEmailClient):
    def __init__(self, session: _FakeSession | None = None) -> None:
        super().__init__(session, provider_status_code=202)


def _request(**overrides) -> EmailSendCreateRequest:
    values = {
        "organisation_id": UUID("11111111-1111-1111-1111-111111111111"),
        "requested_by_user_id": UUID("22222222-2222-2222-2222-222222222222"),
        "to": [EmailSendAddress(email="buyer@example.com")],
        "subject": "Follow-up",
        "body": "Thanks for joining.",
        "sender_email": "seller@contoso.com",
        "idempotency_key": "email-123",
    }
    values.update(overrides)
    return EmailSendCreateRequest(**values)


def _payload(request: EmailSendCreateRequest) -> dict:
    return {
        "sender_email": request.sender_email,
        "to": [recipient.model_dump() for recipient in request.to],
        "cc": [recipient.model_dump() for recipient in request.cc],
        "bcc": [recipient.model_dump() for recipient in request.bcc],
        "subject": request.subject,
        "body": request.body,
        "body_content_type": request.body_content_type,
        "save_to_sent_items": request.save_to_sent_items,
    }


@pytest.mark.asyncio
async def test_send_email_audits_sending_before_graph_and_marks_sent_after_202() -> None:
    session = _FakeSession()
    graph_client = _FakeGraphClient(session)
    service = EmailSendService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
    )

    result = await service.send_email(_request())

    audit_row = session.added[0]
    assert audit_row.status == IntegrationActivityRequestStatus.SENT.value
    assert audit_row.activity_type == "send_email"
    assert audit_row.request_payload["sender_email"] == "seller@contoso.com"
    assert audit_row.title == "Follow-up"
    assert audit_row.participants[0]["kind"] == "to"
    assert audit_row.provider_status_code == 202
    assert audit_row.linked_record_type is None
    assert audit_row.linked_record_id is None
    assert [entry["status"] for entry in audit_row.history] == ["PENDING", "SENDING", "SENT"]
    assert result.status == "sent"
    assert result.request_id == audit_row.request_id
    assert result.provider_status_code == 202
    assert result.linked_record_type is None
    assert result.linked_record_id is None
    assert result.provider_resource_id is None
    assert session.committed == 2
    assert graph_client.requests[0].idempotency_key == "email-123"


@pytest.mark.asyncio
async def test_send_email_uses_gmail_client_and_marks_sent_after_200() -> None:
    session = _FakeSession()
    gmail_client = _FakeEmailClient(
        session,
        provider_status_code=200,
        provider_resource_id="gmail-message-1",
    )
    service = EmailSendService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(IntegrationType.GOOGLE),
        provider_clients={IntegrationType.GOOGLE: gmail_client},
    )

    result = await service.send_email(_request(sender_email="seller@gmail.com"))

    audit_row = session.added[0]
    assert audit_row.status == IntegrationActivityRequestStatus.SENT.value
    assert audit_row.provider_status_code == 200
    assert audit_row.provider_resource_id == "gmail-message-1"
    assert result.provider_status_code == 200
    assert result.provider_resource_id == "gmail-message-1"
    assert gmail_client.requests[0].sender_email == "seller@gmail.com"


@pytest.mark.asyncio
async def test_send_email_reuses_existing_sent_idempotency_key_without_resending() -> None:
    request = _request()
    existing = SimpleNamespace(
        request_id=UUID("44444444-4444-4444-4444-444444444444"),
        status=IntegrationActivityRequestStatus.SENT.value,
        request_payload=_payload(request),
        provider_status_code=202,
        linked_record_type=None,
        linked_record_id=None,
        provider_resource_id=None,
    )
    session = _FakeSession(existing)
    graph_client = _FakeGraphClient()
    service = EmailSendService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
    )

    result = await service.send_email(request)

    assert result.status == "sent"
    assert result.request_id == existing.request_id
    assert result.provider_status_code == 202
    assert graph_client.requests == []


@pytest.mark.asyncio
async def test_send_email_blocks_unknown_sending_replay() -> None:
    request = _request()
    existing = SimpleNamespace(
        request_id=UUID("44444444-4444-4444-4444-444444444444"),
        status=IntegrationActivityRequestStatus.SENDING.value,
        request_payload=_payload(request),
    )
    service = EmailSendService(
        _FakeSession(existing),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=_FakeGraphClient(),
    )

    with pytest.raises(EmailSendError) as exc_info:
        await service.send_email(request)

    assert exc_info.value.code == "email_send_status_unknown"


@pytest.mark.asyncio
async def test_send_email_blocks_failed_replay_without_duplicate_insert() -> None:
    request = _request()
    existing = SimpleNamespace(
        request_id=UUID("44444444-4444-4444-4444-444444444444"),
        status=IntegrationActivityRequestStatus.FAILED.value,
        request_payload=_payload(request),
    )
    session = _FakeSession(existing)
    graph_client = _FakeGraphClient()
    service = EmailSendService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await service.send_email(request)

    assert exc_info.value.code == "email_send_replay_blocked"
    assert session.added == []
    assert graph_client.requests == []


@pytest.mark.asyncio
async def test_send_email_rejects_reused_idempotency_key_for_different_payload() -> None:
    request = _request()
    existing = SimpleNamespace(
        request_id=UUID("44444444-4444-4444-4444-444444444444"),
        status=IntegrationActivityRequestStatus.SENT.value,
        request_payload={**_payload(request), "subject": "Different subject"},
        provider_status_code=202,
        linked_record_type=None,
        linked_record_id=None,
        provider_resource_id=None,
    )
    graph_client = _FakeGraphClient()
    service = EmailSendService(
        _FakeSession(existing),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await service.send_email(request)

    assert exc_info.value.code == "email_send_idempotency_conflict"
    assert graph_client.requests == []


def test_email_idempotency_key_includes_sender_and_recipient_set() -> None:
    request = _request(idempotency_key=None)
    other_sender = request.model_copy(update={"sender_email": "other@contoso.com"})
    other_to = request.model_copy(update={"to": [EmailSendAddress(email="other@example.com")]})
    moved_to_bcc = request.model_copy(
        update={
            "to": [EmailSendAddress(email="archive@example.com")],
            "bcc": [EmailSendAddress(email="buyer@example.com")],
        }
    )
    html_body = request.model_copy(update={"body_content_type": "HTML"})

    assert derive_email_idempotency_key(request) != derive_email_idempotency_key(other_sender)
    assert derive_email_idempotency_key(request) != derive_email_idempotency_key(other_to)
    assert derive_email_idempotency_key(request) != derive_email_idempotency_key(moved_to_bcc)
    assert derive_email_idempotency_key(request) != derive_email_idempotency_key(html_body)
