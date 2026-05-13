"""Tests for Gmail message payloads and transport behavior."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest
from src.service.email_sends.gmail_client import (
    GmailEmailSendClient,
    build_gmail_send_message_payload,
)
from src.service.email_sends.models import EmailSendAddress, EmailSendCreateRequest


def _request(**overrides) -> EmailSendCreateRequest:
    values = {
        "organisation_id": "11111111-1111-1111-1111-111111111111",
        "requested_by_user_id": "22222222-2222-2222-2222-222222222222",
        "sender_email": "seller@example.com",
        "to": [EmailSendAddress(email="buyer@example.com", name="Buyer")],
        "cc": [EmailSendAddress(email="champion@example.com")],
        "bcc": [EmailSendAddress(email="archive@example.com")],
        "subject": "Follow-up",
        "body": "Thanks for joining.",
        "save_to_sent_items": True,
    }
    values.update(overrides)
    return EmailSendCreateRequest(**values)


def _decode_raw_message(payload: dict[str, str]):
    raw = payload["raw"]
    padding = "=" * (-len(raw) % 4)
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + padding))


def test_build_gmail_payload_maps_recipients_subject_and_text_body() -> None:
    message = _decode_raw_message(build_gmail_send_message_payload(_request()))

    assert message["From"] == "seller@example.com"
    assert message["To"] == "Buyer <buyer@example.com>"
    assert message["Cc"] == "champion@example.com"
    assert message["Bcc"] == "archive@example.com"
    assert message["Subject"] == "Follow-up"
    assert message.get_content().strip() == "Thanks for joining."


def test_build_gmail_payload_uses_html_body_content() -> None:
    message = _decode_raw_message(
        build_gmail_send_message_payload(
            _request(body="<p>Thanks</p>", body_content_type="HTML"),
        )
    )

    assert message.get_content_type() == "text/html"
    assert message.get_content().strip() == "<p>Thanks</p>"


@pytest.mark.asyncio
async def test_send_mail_posts_messages_send_through_token_retry(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_retry(token_info, request_fn):
        return await request_fn("access-token")

    async def fake_gmail_post(client, endpoint, *, json_body):
        calls.append((endpoint, json_body))
        return {"id": "gmail-message-1"}

    monkeypatch.setattr("src.service.email_sends.gmail_client.call_with_token_retry", fake_retry)
    monkeypatch.setattr("src.service.email_sends.gmail_client.gmail_api_post", fake_gmail_post)

    result = await GmailEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert result.provider_status_code == 200
    assert result.provider_resource_id == "gmail-message-1"
    assert calls == [
        ("/gmail/v1/users/me/messages/send", build_gmail_send_message_payload(_request()))
    ]


@pytest.mark.asyncio
async def test_send_mail_translates_gmail_permission_errors(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError
    from src.service.integrations.api_error_utils import IntegrationPermissionError

    async def fake_retry(token_info, request_fn):
        raise IntegrationPermissionError(
            "missing scope",
            status_code=403,
            provider="google",
            endpoint="/gmail/v1/users/me/messages/send",
        )

    monkeypatch.setattr("src.service.email_sends.gmail_client.call_with_token_retry", fake_retry)

    with pytest.raises(EmailSendError) as exc_info:
        await GmailEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_send_scope_required"


@pytest.mark.asyncio
async def test_send_mail_translates_revoked_gmail_tokens(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError
    from src.service.integrations.sync_health import AuthTokenRevokedError

    async def fake_retry(token_info, request_fn):
        raise AuthTokenRevokedError("revoked")

    monkeypatch.setattr("src.service.email_sends.gmail_client.call_with_token_retry", fake_retry)

    with pytest.raises(EmailSendError) as exc_info:
        await GmailEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_reauth_required"


@pytest.mark.asyncio
async def test_send_mail_translates_gmail_provider_failures(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError

    async def fake_retry(token_info, request_fn):
        request = httpx.Request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        )
        response = httpx.Response(500, request=request, text="boom")
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr("src.service.email_sends.gmail_client.call_with_token_retry", fake_retry)

    with pytest.raises(EmailSendError) as exc_info:
        await GmailEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_send_provider_error"
