"""Tests for email sender integration resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.email_sends.models import EmailSendError
from src.service.email_sends.resolver import EmailIntegrationResolver


class _Session:
    def __init__(self, integrations: list[KnowledgeIntegration]) -> None:
        self.integrations = integrations

    async def execute(self, _statement):
        integrations = self.integrations

        class _Scalars:
            def all(self):
                return integrations

        class _Result:
            def scalars(self):
                return _Scalars()

        return _Result()


def _integration(**overrides) -> KnowledgeIntegration:
    values = {
        "integration_id": uuid4(),
        "organisation_id": UUID("11111111-1111-1111-1111-111111111111"),
        "created_by_user_id": UUID("22222222-2222-2222-2222-222222222222"),
        "integration_type": IntegrationType.OUTLOOK_EMAIL,
        "integration_email": "seller@contoso.com",
        "is_active": True,
        "requires_reauth": False,
        "credentials": {"refresh_token": "refresh-token"},
        "scopes": ["User.Read", "Mail.Read", "Mail.Send"],
        "created_at": datetime(2026, 5, 13, tzinfo=UTC),
    }
    values.update(overrides)
    return KnowledgeIntegration(**values)


@pytest.mark.asyncio
async def test_resolve_requires_outlook_email_connection() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session([]),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_integration_required"


@pytest.mark.asyncio
async def test_resolve_requires_mail_send_scope() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session([_integration(scopes=["User.Read", "Mail.Read"])]),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_send_scope_required"


@pytest.mark.asyncio
async def test_resolve_requires_sender_selection_for_multiple_writable_mailboxes() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session(
                [
                    _integration(integration_email="seller@contoso.com"),
                    _integration(integration_email="ae@contoso.com"),
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_sender_selection_required"


@pytest.mark.asyncio
async def test_resolve_accepts_explicit_sender_owned_by_user() -> None:
    resolver = EmailIntegrationResolver()

    resolved = await resolver.resolve(
        _Session([_integration(integration_email="seller@contoso.com")]),
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        sender_email="seller@contoso.com",
    )

    assert resolved.integration.integration_email == "seller@contoso.com"


@pytest.mark.asyncio
async def test_resolve_accepts_explicit_gmail_sender_owned_by_user() -> None:
    resolver = EmailIntegrationResolver()

    resolved = await resolver.resolve(
        _Session(
            [
                _integration(
                    integration_type=IntegrationType.GMAIL,
                    integration_email="seller@gmail.com",
                    scopes=[
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.send",
                    ],
                )
            ]
        ),
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        sender_email="seller@gmail.com",
    )

    assert resolved.integration.integration_type == IntegrationType.GMAIL
    assert resolved.integration.integration_email == "seller@gmail.com"


@pytest.mark.asyncio
async def test_resolve_requires_sender_selection_across_gmail_and_outlook() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session(
                [
                    _integration(integration_email="seller@contoso.com"),
                    _integration(
                        integration_type=IntegrationType.GMAIL,
                        integration_email="seller@gmail.com",
                        scopes=[
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/gmail.send",
                        ],
                    ),
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_sender_selection_required"


@pytest.mark.asyncio
async def test_resolve_rejects_gmail_missing_send_scope() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session(
                [
                    _integration(
                        integration_type=IntegrationType.GMAIL,
                        integration_email="seller@gmail.com",
                        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                    )
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_send_scope_required"


@pytest.mark.asyncio
async def test_resolve_requires_reauth_for_unusable_gmail_token() -> None:
    resolver = EmailIntegrationResolver()

    with pytest.raises(EmailSendError) as exc_info:
        await resolver.resolve(
            _Session(
                [
                    _integration(
                        integration_type=IntegrationType.GMAIL,
                        integration_email="seller@gmail.com",
                        credentials={},
                        scopes=[
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/gmail.send",
                        ],
                    )
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "email_reauth_required"
