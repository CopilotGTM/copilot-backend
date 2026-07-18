"""Focused contracts for provider-neutral mailbox draft review state."""

from __future__ import annotations

from src.service.email_sends.gmail_client import (
    build_gmail_send_message_payload,
    normalize_gmail_draft,
)
from src.service.email_sends.microsoft_graph_client import (
    build_microsoft_draft_payload,
    normalize_microsoft_draft,
)
from src.service.email_sends.models import EmailDraftSaveRequest, EmailSendAddress


def _request(*, draft_id: str | None = None) -> EmailDraftSaveRequest:
    return EmailDraftSaveRequest(
        organisation_id="11111111-1111-1111-1111-111111111111",
        requested_by_user_id="22222222-2222-2222-2222-222222222222",
        sender_email="seller@example.com",
        to=[EmailSendAddress(email="buyer@example.com", name="Buyer")],
        cc=[EmailSendAddress(email="champion@example.com")],
        subject="Proposal follow-up",
        body="Please review the proposal.",
        draft_id=draft_id,
    )


def test_gmail_draft_round_trip_produces_review_hash() -> None:
    """Decoded provider state supplies the hash used for send approval."""
    request = _request(draft_id="draft-1")

    snapshot = normalize_gmail_draft(
        {"id": "draft-1", "message": build_gmail_send_message_payload(request)}
    )

    assert snapshot.draft_id == "draft-1"
    assert snapshot.sender_email == "seller@example.com"
    assert [recipient.email for recipient in snapshot.to] == ["buyer@example.com"]
    assert snapshot.subject == "Proposal follow-up"
    assert snapshot.body.strip() == "Please review the proposal."
    assert len(snapshot.content_hash) == 64


def test_outlook_draft_payload_and_snapshot_cover_editable_fields() -> None:
    """Graph drafts expose editable recipients, content, and mailbox link."""
    request = _request(draft_id="draft-2")
    payload = build_microsoft_draft_payload(request)
    payload.update(
        {
            "id": "draft-2",
            "isDraft": True,
            "from": {"emailAddress": {"address": "seller@example.com"}},
            "webLink": "https://outlook.example/draft-2",
        }
    )

    snapshot = normalize_microsoft_draft(payload)

    assert payload["toRecipients"][0]["emailAddress"]["address"] == "buyer@example.com"
    assert snapshot.draft_id == "draft-2"
    assert snapshot.cc[0].email == "champion@example.com"
    assert snapshot.subject == "Proposal follow-up"
    assert snapshot.web_link == "https://outlook.example/draft-2"
    assert len(snapshot.content_hash) == 64
