"""Tests for Outlook sendMail payloads and transport behavior."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from src.service.email_sends.microsoft_graph_client import (
    MicrosoftEmailSendClient,
    build_microsoft_send_mail_payload,
)
from src.service.email_sends.models import EmailSendAddress, EmailSendCreateRequest


def _request(**overrides) -> EmailSendCreateRequest:
    values = {
        "organisation_id": "11111111-1111-1111-1111-111111111111",
        "requested_by_user_id": "22222222-2222-2222-2222-222222222222",
        "to": [EmailSendAddress(email="buyer@example.com", name="Buyer")],
        "cc": [EmailSendAddress(email="champion@example.com")],
        "bcc": [EmailSendAddress(email="archive@example.com")],
        "subject": "Follow-up",
        "body": "Thanks for joining.",
        "save_to_sent_items": True,
    }
    values.update(overrides)
    return EmailSendCreateRequest(**values)


def test_build_microsoft_send_mail_payload_maps_recipients_subject_and_body() -> None:
    payload = build_microsoft_send_mail_payload(_request())

    assert payload == {
        "message": {
            "subject": "Follow-up",
            "body": {"contentType": "Text", "content": "Thanks for joining."},
            "toRecipients": [{"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}}],
            "ccRecipients": [
                {
                    "emailAddress": {
                        "address": "champion@example.com",
                        "name": "champion@example.com",
                    }
                }
            ],
            "bccRecipients": [
                {
                    "emailAddress": {
                        "address": "archive@example.com",
                        "name": "archive@example.com",
                    }
                }
            ],
        },
        "saveToSentItems": True,
    }


@pytest.mark.asyncio
async def test_send_mail_uses_empty_body_202_graph_helper(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_retry(token_info, request_fn):
        return await request_fn("access-token")

    async def fake_post_no_content(client, endpoint, *, json_body):
        calls.append((endpoint, json_body))
        return None

    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.graph_api_post_no_content",
        fake_post_no_content,
    )

    await MicrosoftEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert calls == [("/v1.0/me/sendMail", build_microsoft_send_mail_payload(_request()))]


@pytest.mark.asyncio
async def test_send_mail_translates_outlook_permission_errors(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError
    from src.service.integrations.api_error_utils import IntegrationPermissionError

    async def fake_retry(token_info, request_fn):
        raise IntegrationPermissionError(
            "missing scope",
            status_code=403,
            provider="microsoft",
            endpoint="/v1.0/me/sendMail",
        )

    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await MicrosoftEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_send_scope_required"


@pytest.mark.asyncio
async def test_send_mail_translates_provider_failures(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError

    async def fake_retry(token_info, request_fn):
        request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/me/sendMail")
        response = httpx.Response(500, request=request, text="boom")
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await MicrosoftEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_send_provider_error"


@pytest.mark.asyncio
async def test_send_mail_includes_safe_outlook_provider_error_summary(monkeypatch) -> None:
    from src.service.email_sends.models import EmailSendError

    async def fake_retry(token_info, request_fn):
        request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/me/sendMail")
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "At least one recipient is not valid."}},
        )
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await MicrosoftEmailSendClient().send_mail(SimpleNamespace(), _request())

    assert exc_info.value.code == "email_send_provider_error"
    assert "Outlook Email rejected the send request" in exc_info.value.message
    assert "At least one recipient is not valid" in exc_info.value.message
