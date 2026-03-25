from src.domain.models.organisation_tables.sync import IntegrationType
from src.workers.integrations_worker.tasks.calendar.integration_task import (
    _resolve_provider_config_key,
)


def test_google_calendar_ignores_stale_custom_metadata_provider_key() -> None:
    value = _resolve_provider_config_key(
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        connection_metadata={"provider_config_key": "custom-google_calendar"},
    )

    assert value == "google-calendar"


def test_google_calendar_accepts_valid_google_calendar_metadata_key() -> None:
    value = _resolve_provider_config_key(
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        connection_metadata={"provider_config_key": "google-calendar"},
    )

    assert value == "google-calendar"
