"""Privacy and replay contracts for durable email events."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email, EmailDirection
from src.service.automation.producers.email import EmailEventAdapter


def integration(organisation_id):
    """Build the integration fixture used by the surrounding regression cases."""
    return KnowledgeIntegration(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        name="Mailbox",
        credentials={},
    )


def email(organisation_id, integration_id, *, direction=EmailDirection.INBOUND, draft=False):
    """Build the email fixture used by the surrounding regression cases."""
    return Email(
        organisation_id=organisation_id,
        integration_id=integration_id,
        folder_id=uuid4(),
        external_message_id="provider-message-1",
        from_email="Seller@Example.COM",
        to_emails=["Buyer@example.com"],
        cc_emails=["ALLY@example.com"],
        subject="Confidential roadmap",
        body_text="secret body",
        body_html="<p>secret body</p>",
        snippet="secret snippet",
        attachments=[{"name": "secret.pdf", "bytes": "not-safe"}],
        received_at=datetime(2026, 8, 5, 9, tzinfo=UTC),
        sent_at=datetime(2026, 8, 5, 8, tzinfo=UTC),
        direction=direction,
        is_draft=draft,
        has_attachments=True,
    )


def test_email_envelope_references_content_without_copying_sensitive_content() -> None:
    """Verify the contract that email envelope references content without copying sensitive content."""
    organisation_id = uuid4()
    mailbox = integration(organisation_id)
    message = email(organisation_id, mailbox.integration_id)

    audience_user_id = uuid4()
    envelope = EmailEventAdapter(organisation_id).from_email(
        message,
        integration=mailbox,
        provider="outlook",
        source_mode="incremental_sync",
        audience_user_ids={audience_user_id},
    )

    assert envelope is not None
    assert envelope.event_key == "email.message.received"
    assert envelope.record["sender_email"] == "seller@example.com"
    assert envelope.record["recipient_emails"] == ["buyer@example.com", "ally@example.com"]
    assert envelope.audience_user_ids == (audience_user_id,)
    assert envelope.references == {"email_id": str(message.email_id)}
    serialized = envelope.model_dump_json()
    for forbidden in ("secret body", "secret snippet", "secret.pdf", "Confidential roadmap"):
        assert forbidden not in serialized


def test_sent_identity_is_deterministic_and_drafts_emit_nothing() -> None:
    """Verify the contract that sent identity is deterministic and drafts emit nothing."""
    organisation_id = uuid4()
    mailbox = integration(organisation_id)
    sent = email(organisation_id, mailbox.integration_id, direction=EmailDirection.OUTBOUND)
    adapter = EmailEventAdapter(organisation_id)

    first = adapter.from_email(
        sent,
        integration=mailbox,
        provider="outlook",
        source_mode="system",
    )
    replay = adapter.from_email(
        sent,
        integration=mailbox,
        provider="outlook",
        source_mode="system",
    )
    draft = email(
        organisation_id,
        mailbox.integration_id,
        direction=EmailDirection.OUTBOUND,
        draft=True,
    )

    assert first is not None and first.event_key == "email.message.sent"
    assert replay is not None and replay.event_id == first.event_id
    assert replay.idempotency_key == first.idempotency_key
    assert (
        adapter.from_email(
            draft,
            integration=mailbox,
            provider="outlook",
            source_mode="system",
        )
        is None
    )
