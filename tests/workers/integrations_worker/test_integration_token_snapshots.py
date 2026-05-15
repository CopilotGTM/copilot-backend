from __future__ import annotations

from uuid import uuid4

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.email_delta_sync_service import (
    IntegrationSyncData as EmailIntegrationSyncData,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.tasks.calendar.integration_task import (
    IntegrationFetchData as CalendarIntegrationFetchData,
)
from src.workers.integrations_worker.tasks.calendar.org_task import (
    IntegrationFetchData as CalendarOrgFetchData,
)
from src.workers.integrations_worker.tasks.messaging.channels_integration_task import (
    IntegrationFetchData as MessagingIntegrationFetchData,
)
from src.workers.integrations_worker.tasks.messaging.channels_org_task import (
    IntegrationFetchData as MessagingOrgFetchData,
)


def test_detached_integration_snapshots_preserve_scopes_for_token_info() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    scopes = ["offline_access", "User.Read", "Calendars.Read"]
    credentials = {"access_token": "access-token", "refresh_token": "refresh-token"}

    snapshots = [
        CalendarOrgFetchData(
            integration_id=integration_id,
            connection_id="calendar-connection",
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
            integration_type_value=IntegrationType.OUTLOOK_CALENDAR.value,
            provider="outlook",
            is_initial=False,
            last_synced_at=None,
            calendar_delta_link="delta",
            credentials=credentials,
            access_token_expires_at=None,
            scopes=scopes,
        ),
        CalendarIntegrationFetchData(
            integration_id=integration_id,
            connection_id="calendar-connection",
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
            integration_type_value=IntegrationType.OUTLOOK_CALENDAR.value,
            provider="outlook",
            is_initial=False,
            last_synced_at=None,
            calendar_delta_link="delta",
            credentials=credentials,
            access_token_expires_at=None,
            scopes=scopes,
        ),
        MessagingOrgFetchData(
            integration_id=integration_id,
            connection_id="messaging-connection",
            integration_type=IntegrationType.TEAMS,
            integration_type_value=IntegrationType.TEAMS.value,
            provider="teams",
            provider_config_key="microsoft-teams",
            is_initial=False,
            last_synced_at=None,
            credentials=credentials,
            access_token_expires_at=None,
            scopes=scopes,
        ),
        MessagingIntegrationFetchData(
            integration_id=integration_id,
            connection_id="messaging-connection",
            integration_type=IntegrationType.TEAMS,
            integration_type_value=IntegrationType.TEAMS.value,
            provider="teams",
            provider_config_key="microsoft-teams",
            is_initial=False,
            last_synced_at=None,
            credentials=credentials,
            access_token_expires_at=None,
            scopes=scopes,
        ),
        EmailIntegrationSyncData(
            integration_id=integration_id,
            integration_type=IntegrationType.OUTLOOK_EMAIL,
            integration_type_value=IntegrationType.OUTLOOK_EMAIL.value,
            connection_id="email-connection",
            provider="outlook",
            provider_key="outlook",
            last_synced_at=None,
            connection_metadata={},
            credentials=credentials,
            access_token_expires_at=None,
            scopes=scopes,
        ),
    ]

    for snapshot in snapshots:
        token_info = IntegrationTokenInfo.from_integration(snapshot, organisation_id)

        assert token_info.integration_id == integration_id
        assert token_info.organisation_id == organisation_id
        assert token_info.scopes == scopes


def test_non_salesforce_token_snapshot_ignores_oauth_base_url_metadata() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    snapshot = EmailIntegrationSyncData(
        integration_id=integration_id,
        integration_type=IntegrationType.GMAIL,
        integration_type_value=IntegrationType.GMAIL.value,
        connection_id="gmail-connection",
        provider="gmail",
        provider_key="gmail",
        last_synced_at=None,
        connection_metadata={"oauth_base_url": "https://evil.example.com"},
        credentials={"access_token": "access-token", "refresh_token": "refresh-token"},
        access_token_expires_at=None,
        scopes=["gmail.readonly"],
    )

    token_info = IntegrationTokenInfo.from_integration(snapshot, organisation_id)

    assert token_info.oauth_base_url is None
