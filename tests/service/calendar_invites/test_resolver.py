"""Tests for selecting the correct calendar sender integration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.config.google_integration_scopes import (
    GOOGLE_INTEGRATION_SCOPES,
    GOOGLE_SCOPE_CALENDAR_EVENTS,
)
from src.infra.config.microsoft_integration_scopes import MICROSOFT_INTEGRATION_SCOPES
from src.service.calendar_invites.models import CalendarInviteError
from src.service.calendar_invites.resolver import CalendarIntegrationResolver


class _FakeScalars:
    """Small cursor object stub for SQLAlchemy-like scalar result APIs."""

    def __init__(self, rows: list[KnowledgeIntegration]) -> None:
        self.rows = rows

    def all(self) -> list[KnowledgeIntegration]:
        """Return all rows for callers that iterate scalars().all()."""
        return self.rows


class _FakeResult:
    """Query result wrapper used by fake session execute paths."""

    def __init__(self, rows: list[KnowledgeIntegration]) -> None:
        self.rows = rows

    def scalars(self) -> _FakeScalars:
        """Return scalar rows and preserve a familiar session result shape."""
        return _FakeScalars(self.rows)


class _FakeSession:
    """Session stub for resolver selection tests."""

    def __init__(self, rows: list[KnowledgeIntegration]) -> None:
        self.rows = rows

    async def execute(self, _stmt):
        """Return deterministic rows for scope/email selection tests."""
        return _FakeResult(self.rows)


def _integration(
    *,
    email: str,
    scopes: list[str] | None = None,
    requires_reauth: bool = False,
    is_active: bool = True,
    integration_type: IntegrationType = IntegrationType.MICROSOFT,
) -> KnowledgeIntegration:
    """Create a test provider integration with deterministic identity and grants."""
    default_scopes = (
        list(GOOGLE_INTEGRATION_SCOPES[IntegrationType.GOOGLE_CALENDAR])
        if integration_type == IntegrationType.GOOGLE
        else list(MICROSOFT_INTEGRATION_SCOPES[IntegrationType.OUTLOOK_CALENDAR])
    )
    return KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_type=integration_type,
        name=f"{integration_type.value} {email}",
        integration_email=email,
        is_active=is_active,
        requires_reauth=requires_reauth,
        scopes=scopes or default_scopes,
        credentials={"access_token": "token"},
        created_at=datetime(2026, 5, 12, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_resolver_requires_sender_selection_when_multiple_writable_accounts() -> None:
    """Multiple writable integrations should request explicit sender selection."""
    # Contract: with >1 writable accounts and no explicit sender, resolver asks
    # caller to pick sender.
    resolver = CalendarIntegrationResolver()

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession(
                [
                    _integration(email="seller@contoso.com"),
                    _integration(email="ae@contoso.com"),
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "calendar_sender_selection_required"
    assert "seller@contoso.com" in exc_info.value.message
    assert "ae@contoso.com" in exc_info.value.message


@pytest.mark.asyncio
async def test_resolver_uses_selected_sender_email() -> None:
    """Explicit sender_email must select the matching integration deterministically."""
    # Contract: explicit sender_email filters candidate integrations deterministically.
    selected = _integration(email="seller@contoso.com")
    resolver = CalendarIntegrationResolver()

    resolved = await resolver.resolve(
        _FakeSession([selected, _integration(email="ae@contoso.com")]),
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        sender_email="seller@contoso.com",
    )

    assert resolved.integration.integration_id == selected.integration_id


@pytest.mark.asyncio
async def test_resolver_rejects_selected_account_without_write_scope() -> None:
    """Selected account without write scope should return reconnect guidance."""
    # Contract: selected account with read-only scope returns write scope action.
    resolver = CalendarIntegrationResolver()

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession([_integration(email="seller@contoso.com", scopes=["Calendars.Read"])]),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            sender_email="seller@contoso.com",
        )

    assert exc_info.value.code == "calendar_write_scope_required"


@pytest.mark.asyncio
async def test_resolver_rejects_selected_account_requiring_reauth() -> None:
    """Selected account requiring reauth should not fall back to another sender."""
    resolver = CalendarIntegrationResolver()

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession(
                [
                    _integration(email="seller@contoso.com", requires_reauth=True),
                    _integration(email="ae@contoso.com"),
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            sender_email="seller@contoso.com",
        )

    assert exc_info.value.code == "calendar_reauth_required"


@pytest.mark.asyncio
async def test_resolver_returns_reauth_for_inactive_reauth_calendar_connection() -> None:
    """Inactive reauth rows should produce reconnect guidance, not disconnected state."""
    resolver = CalendarIntegrationResolver()

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession(
                [
                    _integration(
                        email="seller@contoso.com",
                        is_active=False,
                        requires_reauth=True,
                    )
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "calendar_reauth_required"


@pytest.mark.asyncio
async def test_resolver_uses_selected_google_sender() -> None:
    """Explicit Google sender should resolve to the Google provider integration."""
    selected = _integration(
        email="seller@gmail.com",
        integration_type=IntegrationType.GOOGLE,
    )
    resolver = CalendarIntegrationResolver()

    resolved = await resolver.resolve(
        _FakeSession([_integration(email="ae@contoso.com"), selected]),
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        sender_email="seller@gmail.com",
    )

    assert resolved.integration.integration_id == selected.integration_id
    assert resolved.provider == "google"


@pytest.mark.asyncio
async def test_resolver_requires_selection_for_multiple_provider_writable_accounts() -> None:
    """Writable Outlook and Google accounts should still require sender selection."""
    resolver = CalendarIntegrationResolver()

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession(
                [
                    _integration(email="seller@contoso.com"),
                    _integration(
                        email="seller@gmail.com",
                        integration_type=IntegrationType.GOOGLE,
                    ),
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )

    assert exc_info.value.code == "calendar_sender_selection_required"
    assert "seller@contoso.com" in exc_info.value.message
    assert "seller@gmail.com" in exc_info.value.message


@pytest.mark.asyncio
async def test_resolver_rejects_selected_google_account_without_write_scope() -> None:
    """Selected Google Calendar sender must have event-write scope."""
    resolver = CalendarIntegrationResolver()
    read_only_google_scopes = [
        scope
        for scope in GOOGLE_INTEGRATION_SCOPES[IntegrationType.GOOGLE_CALENDAR]
        if scope != GOOGLE_SCOPE_CALENDAR_EVENTS
    ]

    with pytest.raises(CalendarInviteError) as exc_info:
        await resolver.resolve(
            _FakeSession(
                [
                    _integration(
                        email="seller@gmail.com",
                        scopes=read_only_google_scopes,
                        integration_type=IntegrationType.GOOGLE,
                    )
                ]
            ),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            sender_email="seller@gmail.com",
        )

    assert exc_info.value.code == "calendar_write_scope_required"
