from src.domain.models.organisation_tables.sync import IntegrationScope, IntegrationType
from src.infra.config.integrations_metadata import (
    INTEGRATION_METADATA,
    AvailabilityStatus,
)
from src.service.integrations.providers.provider_factory import ProviderFactory
from src.service.integrations.providers.salesforce_provider import SalesforceProvider


def test_provider_factory_returns_salesforce_provider() -> None:
    provider = ProviderFactory.get_provider(IntegrationType.SALESFORCE)

    assert isinstance(provider, SalesforceProvider)
    assert provider.provider_key == "salesforce"


def test_salesforce_metadata_is_beta_and_org_scoped() -> None:
    metadata = INTEGRATION_METADATA[IntegrationType.SALESFORCE]

    assert metadata.availability_status == AvailabilityStatus.BETA
    assert metadata.default_scope == IntegrationScope.ORGANIZATION
    assert metadata.allow_multiple_connections is False
    assert metadata.oauth_scopes == [
        "refresh_token",
        "offline_access",
        "api",
        "openid",
        "profile",
        "email",
    ]
