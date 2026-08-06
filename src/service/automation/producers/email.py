"""Privacy-bounded email received/sent event adapter."""

from __future__ import annotations

from datetime import UTC
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email, EmailDirection
from src.service.automation.events.contracts import AutomationEventEnvelope


class EmailEventAdapter:
    """Build the one durable arrival/send fact for a newly persisted email."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def from_email(
        self,
        email: Email,
        *,
        integration: KnowledgeIntegration,
        provider: str,
        source_mode: str,
        audience_user_ids: set[UUID] | tuple[UUID, ...] = (),
    ) -> AutomationEventEnvelope | None:
        """Build the canonical event or contract from email."""
        if (
            email.organisation_id != self.organisation_id
            or integration.organisation_id != self.organisation_id
            or email.integration_id != integration.integration_id
        ):
            raise ValueError("Email producer boundary does not match organisation/integration")
        if email.is_deleted or email.is_draft:
            return None
        direction = _value(email.direction)
        if direction not in {EmailDirection.INBOUND.value, EmailDirection.OUTBOUND.value}:
            raise ValueError("Email direction is not supported")
        event_key = (
            "email.message.received"
            if direction == EmailDirection.INBOUND.value
            else "email.message.sent"
        )
        event_id = uuid5(
            NAMESPACE_URL,
            f"copilotgtm:{self.organisation_id}:{event_key}:{email.email_id}",
        )
        occurred_at = email.received_at if direction == "inbound" else email.sent_at
        occurred_at = occurred_at or email.received_at
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        recipients = list(
            dict.fromkeys(
                address.strip().casefold()
                for address in (*email.to_emails, *email.cc_emails, *email.bcc_emails)
                if address and address.strip()
            )
        )
        sender_email = email.from_email.strip().casefold()
        sender_domain = sender_email.rpartition("@")[2] or None
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": event_id,
                "organisation_id": self.organisation_id,
                "event_key": event_key,
                "route": {"mode": "match"},
                "occurred_at": occurred_at,
                "source": {
                    "domain": "email",
                    "system": provider,
                    "operation": "signal",
                    "mode": source_mode,
                    "source_event_id": str(email.email_id),
                },
                "actor": {"type": "integration", "id": str(integration.integration_id)},
                "subject": {"type": "email", "id": str(email.email_id)},
                "record": {
                    "id": str(email.email_id),
                    "direction": direction,
                    "sender_email": sender_email,
                    "sender_domain": sender_domain,
                    "recipient_emails": recipients,
                    "has_attachments": bool(email.has_attachments),
                    "thread_id": email.thread_id,
                    "owner_id": str(integration.created_by_user_id),
                    "deal_id": str(email.deal_id) if email.deal_id else None,
                },
                "attributes": {"direction": direction},
                "references": {"email_id": str(email.email_id)},
                "audience_user_ids": sorted(audience_user_ids, key=str),
                "correlation_id": str(email.email_id),
                "idempotency_key": f"{event_key}:{email.email_id}",
            }
        )


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


__all__ = ["EmailEventAdapter"]
