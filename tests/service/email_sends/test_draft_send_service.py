"""Service-level safety tests for sending reviewed mailbox drafts."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_COMPOSE
from src.service.email_sends.models import (
    EmailDraftSnapshot,
    EmailSendAddress,
    EmailSendError,
)
from src.service.email_sends.service import EmailSendService


class _Resolver:
    async def resolve(self, session, organisation_id, requested_by_user_id, *, sender_email=None):
        """Return one user-owned Gmail integration with draft permission."""
        return SimpleNamespace(
            integration=SimpleNamespace(
                integration_id=UUID(int=3),
                integration_email="seller@example.com",
                integration_type=IntegrationType.GOOGLE,
                scopes=[GOOGLE_SCOPE_GMAIL_COMPOSE],
            ),
            token_info=SimpleNamespace(access_token="token"),
        )


class _Client:
    def __init__(self) -> None:
        self.send_calls = 0

    async def get_draft(self, token_info, draft_id):
        """Return provider content that differs from the approved hash."""
        return EmailDraftSnapshot(
            draft_id=draft_id,
            sender_email="seller@example.com",
            to=[EmailSendAddress(email="buyer@example.com")],
            subject="Proposal",
            body="Current provider content",
            content_hash="b" * 64,
        )

    async def send_draft(self, token_info, draft_id):
        """Count provider sends so the test can prove none occurred."""
        self.send_calls += 1
        return SimpleNamespace(provider_status_code=200, provider_resource_id="message-1")


@pytest.mark.asyncio
async def test_service_rejects_draft_changed_after_review() -> None:
    """A stale approval hash must fail before audit or provider send side effects."""

    client = _Client()
    service = EmailSendService(
        SimpleNamespace(),
        organisation_id=UUID(int=1),
        requested_by_user_id=UUID(int=2),
        integration_resolver=_Resolver(),
        provider_clients={IntegrationType.GOOGLE: client},
    )

    with pytest.raises(EmailSendError) as exc_info:
        await service.send_draft(
            draft_id="draft-1",
            sender_email="seller@example.com",
            expected_content_hash="a" * 64,
        )

    assert exc_info.value.code == "email_draft_changed"
    assert client.send_calls == 0
