"""Tests for callback-scope validation helpers."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.master_tables import OrganisationIntegrationDetails
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations import copilot_callback_service
from src.service.integrations.copilot_callback_service import CopilotCallbackService


@pytest.mark.asyncio
async def test_progress_creation_failure_does_not_block_initial_sync(monkeypatch) -> None:
    """Progress is optional telemetry; its database failure cannot suppress data sync."""
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.GMAIL,
        name="Gmail",
        credentials={},
        connection_id=str(uuid4()),
    )
    dispatched: list[object] = []

    async def fail_start(*_args, **_kwargs):
        raise RuntimeError("progress database unavailable")

    def capture_dispatch(*_args, **kwargs):
        dispatched.append(kwargs.get("initial_sync_id"))

    monkeypatch.setattr(
        copilot_callback_service.InitialSyncProgressService,
        "start_run",
        fail_start,
    )
    monkeypatch.setattr(
        copilot_callback_service.InitialSyncService,
        "trigger_initial_sync",
        capture_dispatch,
    )

    await CopilotCallbackService()._trigger_initial_sync(
        SimpleNamespace(),
        integration,
        enabled_capabilities=[IntegrationType.GMAIL],
    )

    assert dispatched == [None]


def test_validate_required_scopes_allows_granted_gmail_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_allows_send_only_gmail_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/userinfo.profile "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_rejects_missing_gmail_scope() -> None:
    with pytest.raises(ValueError, match="Gmail permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GMAIL,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/userinfo.profile"
            },
        )


def test_validate_required_scopes_rejects_missing_google_meet_scope() -> None:
    with pytest.raises(ValueError, match="Google Meet permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GOOGLE_CALENDAR,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/calendar.readonly"
            },
        )


def test_validate_required_scopes_rejects_missing_google_calendar_events_scope() -> None:
    with pytest.raises(ValueError, match="Google Calendar invite permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GOOGLE_CALENDAR,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/calendar.readonly "
                "https://www.googleapis.com/auth/meetings.space.readonly",
                "refresh_token": "refresh-token",
            },
        )


def test_validate_required_scopes_rejects_missing_hubspot_deal_schema_permission() -> None:
    with pytest.raises(ValueError, match="HubSpot permissions were not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.HUBSPOT,
            {
                "scope": "oauth crm.objects.companies.read crm.objects.contacts.read "
                "crm.objects.deals.read crm.objects.owners.read"
            },
        )


def test_validate_required_scopes_accepts_salesforce_offline_access_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.SALESFORCE,
        {
            "scope": "api offline_access openid profile email",
        },
    )


def test_validate_required_scopes_accepts_microsoft_without_offline_scope_echo() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.OUTLOOK_EMAIL,
        {
            "scope": "User.Read Mail.Read Mail.Send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_rejects_microsoft_without_refresh_token() -> None:
    with pytest.raises(ValueError, match="Microsoft offline access was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            {
                "scope": "User.Read Mail.Read Mail.Send",
            },
        )


def test_validate_required_scopes_accepts_google_without_offline_scope_echo() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_google_parent_default_requires_calendar_but_not_gmail() -> None:
    service = CopilotCallbackService()

    service._validate_required_scopes(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly",
            "refresh_token": "refresh-token",
        },
    )

    assert service._get_enabled_capabilities(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.GOOGLE_CALENDAR]


def test_google_parent_enables_gmail_when_gmail_scopes_are_granted() -> None:
    assert CopilotCallbackService()._get_enabled_capabilities(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.GOOGLE_CALENDAR, IntegrationType.GMAIL]


def test_microsoft_parent_default_enables_email_and_calendar_without_teams() -> None:
    assert CopilotCallbackService()._get_enabled_capabilities(
        IntegrationType.MICROSOFT,
        {
            "scope": "User.Read Mail.Read Mail.Send Calendars.ReadWrite OnlineMeetings.Read",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.OUTLOOK_EMAIL, IntegrationType.OUTLOOK_CALENDAR]


def test_master_integration_mapping_exposes_routing_email() -> None:
    """The master connection index must expose the Gmail routing key."""
    assert "integration_email" in OrganisationIntegrationDetails.model_fields


class _MappingResult:
    """Return the one master mapping supplied by a callback test."""

    def __init__(self, mapping: object | None) -> None:
        self.mapping = mapping

    def scalar_one_or_none(self) -> object | None:
        """Match SQLAlchemy's single-result API."""
        return self.mapping


class _MappingSession:
    """Record callback persistence operations without a database."""

    def __init__(self, mapping: object | None) -> None:
        self.mapping = mapping
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement: object) -> _MappingResult:
        """Return the configured mapping."""
        return _MappingResult(self.mapping)

    def add(self, value: object) -> None:
        """Record an inserted mapping."""
        self.added.append(value)

    async def commit(self) -> None:
        """Record a completed transaction."""
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_save_connection_mapping_normalizes_routing_email(
    monkeypatch, existing: bool
) -> None:
    """OAuth writes must keep the master Gmail routing key normalized."""
    organisation_id = uuid4()
    integration_id = uuid4()
    mapping = (
        SimpleNamespace(
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider_config_key="custom-google",
        )
        if existing
        else None
    )
    session = _MappingSession(mapping)

    async def acquire_mapping_session():
        """Yield the recording master session."""
        yield session

    monkeypatch.setattr(
        copilot_callback_service,
        "acquire_master_async_session",
        acquire_mapping_session,
    )
    integration = KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        integration_scope=IntegrationScope.USER,
        integration_email=" Seller@Example.com ",
        connection_id=str(uuid4()),
        credentials={},
        is_active=True,
    )

    await CopilotCallbackService()._save_connection_mapping(
        str(integration.connection_id),
        integration,
        "custom-google",
    )

    saved = mapping if mapping is not None else session.added[0]
    assert saved.integration_email == "seller@example.com"
    assert session.commits == 1
