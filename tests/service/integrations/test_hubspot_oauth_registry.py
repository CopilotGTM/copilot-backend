from src.domain.models.organisation_tables.sync import IntegrationScope, IntegrationType
from src.infra.config.integrations_metadata import (
    INTEGRATION_METADATA,
    AvailabilityStatus,
)
from src.service.integrations.providers.hubspot_provider import HubSpotProvider
from src.service.integrations.providers.provider_factory import ProviderFactory


def test_provider_factory_returns_hubspot_provider() -> None:
    provider = ProviderFactory.get_provider(IntegrationType.HUBSPOT)

    assert isinstance(provider, HubSpotProvider)
    assert provider.provider_key == "hubspot"


def test_hubspot_metadata_is_beta_and_org_scoped() -> None:
    metadata = INTEGRATION_METADATA[IntegrationType.HUBSPOT]

    assert metadata.availability_status == AvailabilityStatus.BETA
    assert metadata.default_scope == IntegrationScope.ORGANIZATION
    assert metadata.allow_multiple_connections is False
    assert metadata.oauth_scopes == [
        "oauth",
        "crm.objects.companies.read",
        "crm.objects.contacts.read",
        "crm.objects.deals.read",
        "crm.objects.owners.read",
        "crm.schemas.deals.read",
    ]


def test_hubspot_provider_uses_metadata_scopes() -> None:
    provider = HubSpotProvider(IntegrationType.HUBSPOT)

    assert provider._get_scopes() == INTEGRATION_METADATA[IntegrationType.HUBSPOT].oauth_scopes
