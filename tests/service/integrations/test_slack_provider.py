from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.integrations_metadata import INTEGRATION_METADATA
from src.service.integrations.providers.slack_provider import SlackProvider


def test_slack_integration_metadata_matches_provider_requested_scopes() -> None:
    assert (
        INTEGRATION_METADATA[IntegrationType.SLACK].oauth_scopes
        == SlackProvider(IntegrationType.SLACK)._get_scopes()
    )
