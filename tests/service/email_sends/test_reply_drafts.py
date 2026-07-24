"""Service contracts for resolving mailbox-owned email reply drafts."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import Email
from src.service.email_sends.gmail_client import (
    GmailEmailSendClient,
    build_gmail_send_message_payload,
)
from src.service.email_sends.microsoft_graph_client import MicrosoftEmailSendClient
from src.service.email_sends.models import (
    EmailDraftSaveRequest,
    EmailSendAddress,
    EmailSendError,
)
from src.service.email_sends.service import EmailSendService

ORGANISATION_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")
INTEGRATION_ID = UUID("33333333-3333-3333-3333-333333333333")


class _SourceSession:
    """Return one synchronized source while capturing the ownership-filtered query."""

    def __init__(self, source: Email | None) -> None:
        self.source = source
        self.statements: list[object] = []

    async def execute(self, statement):
        """Return the configured source for the service's exact-message lookup."""
        self.statements.append(statement)
        source = self.source

        class _Scalars:
            def first(self):
                """Return the single synchronized source configured for this test."""
                return source

        return SimpleNamespace(scalars=lambda: _Scalars())


def _source(**overrides) -> Email:
    values = {
        "organisation_id": ORGANISATION_ID,
        "integration_id": INTEGRATION_ID,
        "folder_id": UUID("44444444-4444-4444-4444-444444444444"),
        "external_message_id": "provider-message-1",
        "thread_id": "provider-thread-1",
        "internet_message_id": "<source@example.com>",
        "from_email": "buyer@example.com",
        "from_name": "Buyer",
        "to_emails": ["seller@example.com", "observer@example.com"],
        "cc_emails": ["champion@example.com"],
        "subject": "Proposal",
        "received_at": datetime.now(UTC),
        "references": ["<parent@example.com>"],
    }
    values.update(overrides)
    return Email(**values)


def _request(**overrides) -> EmailDraftSaveRequest:
    values = {
        "organisation_id": ORGANISATION_ID,
        "requested_by_user_id": USER_ID,
        "sender_email": "seller@example.com",
        "to": [EmailSendAddress(email="wrong@example.com")],
        "cc": [EmailSendAddress(email="wrong-cc@example.com")],
        "bcc": [EmailSendAddress(email="wrong-bcc@example.com")],
        "subject": "Wrong subject",
        "body": "Thanks, I will review it.",
        "reply_to_internet_message_id": "<source@example.com>",
    }
    values.update(overrides)
    return EmailDraftSaveRequest(**values)


@pytest.mark.asyncio
async def test_reply_resolution_replaces_model_routing_with_source_thread() -> None:
    """A verified source, not model-provided recipients, controls the reply draft."""

    session = _SourceSession(_source())
    service = EmailSendService(
        session,
        organisation_id=ORGANISATION_ID,
        requested_by_user_id=USER_ID,
    )

    request = await service._prepare_reply_draft(
        _request(),
        integration_id=INTEGRATION_ID,
        sender_email="seller@example.com",
    )

    assert [address.email for address in request.to] == ["buyer@example.com"]
    assert [address.email for address in request.cc] == [
        "observer@example.com",
        "champion@example.com",
    ]
    assert request.bcc == []
    assert request.reply_mode == "reply_all"
    assert request.subject == "Re: Proposal"
    assert request.reply_to_external_message_id == "provider-message-1"
    assert request.reply_thread_id == "provider-thread-1"
    assert request.reply_references == ["<parent@example.com>", "<source@example.com>"]
    compiled = str(session.statements[0])
    assert "emails.organisation_id" in compiled
    assert "emails.integration_id" in compiled


@pytest.mark.asyncio
async def test_outbound_follow_up_preserves_original_recipients_without_sender() -> None:
    """A follow-up to the user's sent message must not turn into a reply to self."""

    service = EmailSendService(
        _SourceSession(
            _source(
                from_email="seller@example.com",
                from_name="Seller",
                to_emails=["buyer@example.com", "observer@example.com"],
                cc_emails=["champion@example.com"],
            )
        ),
        organisation_id=ORGANISATION_ID,
        requested_by_user_id=USER_ID,
    )

    request = await service._prepare_reply_draft(
        _request(),
        integration_id=INTEGRATION_ID,
        sender_email="seller@example.com",
    )

    assert [address.email for address in request.to] == [
        "buyer@example.com",
        "observer@example.com",
    ]
    assert [address.email for address in request.cc] == ["champion@example.com"]
    assert request.reply_mode == "reply_all"


@pytest.mark.asyncio
async def test_missing_mailbox_source_fails_instead_of_creating_new_email() -> None:
    """An unresolved reply target must fail closed rather than start a new thread."""

    service = EmailSendService(
        _SourceSession(None),
        organisation_id=ORGANISATION_ID,
        requested_by_user_id=USER_ID,
    )

    with pytest.raises(EmailSendError) as exc_info:
        await service._prepare_reply_draft(
            _request(),
            integration_id=INTEGRATION_ID,
            sender_email="seller@example.com",
        )

    assert exc_info.value.code == "email_reply_source_not_found"


def test_reply_target_and_existing_draft_are_mutually_exclusive() -> None:
    """One save call cannot both create a reply and replace an existing draft."""

    with pytest.raises(ValueError, match="cannot be used together"):
        _request(draft_id="existing-draft")


def test_gmail_reply_draft_includes_thread_and_rfc_headers() -> None:
    """A Gmail reply needs both provider and RFC identities to stay in its thread."""

    request = _request().model_copy(
        update={
            "reply_to_external_message_id": "gmail-message-1",
            "reply_thread_id": "gmail-thread-1",
            "reply_references": ["<parent@example.com>", "<source@example.com>"],
        }
    )

    payload = build_gmail_send_message_payload(request)
    encoded = payload["raw"] + "=" * (-len(payload["raw"]) % 4)
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded))

    assert payload["threadId"] == "gmail-thread-1"
    assert message["In-Reply-To"] == "<source@example.com>"
    assert message["References"] == "<parent@example.com> <source@example.com>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_mode", "action"),
    [("reply", "createReply"), ("reply_all", "createReplyAll")],
)
async def test_outlook_reply_draft_uses_native_graph_operation(
    monkeypatch,
    reply_mode: str,
    action: str,
) -> None:
    """Outlook must use its native reply operation instead of a new message."""

    calls: list[tuple[str, dict]] = []

    async def fake_retry(token_info, request_fn):
        """Run the provider closure with a stable access token."""
        return await request_fn("access-token")

    async def fake_post_json(client, endpoint, *, json_body):
        """Capture the Graph action and return its normalized draft shape."""
        calls.append((endpoint, json_body))
        return {
            "id": "reply-draft-1",
            "isDraft": True,
            "subject": "Re: Proposal",
            "body": {"contentType": "Text", "content": "Thanks, I will review it."},
            "from": {"emailAddress": {"address": "seller@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "buyer@example.com"}}],
        }

    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.email_sends.microsoft_graph_client.graph_api_post_json",
        fake_post_json,
    )

    request = _request(reply_mode=reply_mode).model_copy(
        update={
            "reply_to_external_message_id": "outlook-message-1",
            "reply_thread_id": "outlook-thread-1",
        }
    )
    snapshot = await MicrosoftEmailSendClient().save_draft(SimpleNamespace(), request)

    assert calls == [
        (
            f"/v1.0/me/messages/outlook-message-1/{action}",
            {
                "message": {
                    "body": {
                        "contentType": "Text",
                        "content": "Thanks, I will review it.",
                    }
                }
            },
        )
    ]
    assert snapshot.draft_id == "reply-draft-1"


@pytest.mark.asyncio
async def test_gmail_reply_draft_update_preserves_thread_identity(monkeypatch) -> None:
    """Editing a Gmail reply body must not detach its replacement MIME message."""

    original = _request().model_copy(
        update={
            "subject": "Re: Proposal",
            "reply_thread_id": "gmail-thread-1",
            "reply_references": ["<parent@example.com>", "<source@example.com>"],
        }
    )
    original_payload = build_gmail_send_message_payload(original)
    updated = _request(
        draft_id="reply-draft-1",
        reply_to_internet_message_id=None,
        subject="Model changed this subject",
        body="Updated reply body.",
    )
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def fake_retry(token_info, request_fn):
        """Run the Gmail closure with a stable access token."""
        return await request_fn("access-token")

    async def fake_gmail_request(client, method, endpoint, *, json_body=None, params=None):
        """Return existing and saved provider states while capturing the update."""
        calls.append((method, endpoint, json_body, params))
        if method == "GET" and len(calls) == 1:
            return {"id": "reply-draft-1", "message": original_payload}
        if method == "PUT":
            return {"id": "reply-draft-1"}
        return {
            "id": "reply-draft-1",
            "message": (calls[1][2] or {})["message"],
        }

    monkeypatch.setattr(
        "src.service.email_sends.gmail_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.email_sends.gmail_client._gmail_json_request",
        fake_gmail_request,
    )

    snapshot = await GmailEmailSendClient().save_draft(SimpleNamespace(), updated)

    saved_message = (calls[1][2] or {})["message"]
    encoded = saved_message["raw"] + "=" * (-len(saved_message["raw"]) % 4)
    mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded))
    assert saved_message["threadId"] == "gmail-thread-1"
    assert mime["Subject"] == "Re: Proposal"
    assert mime["In-Reply-To"] == "<source@example.com>"
    assert mime["References"] == "<parent@example.com> <source@example.com>"
    assert mime.get_content().strip() == "Updated reply body."
    assert snapshot.subject == "Re: Proposal"
