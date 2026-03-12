from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.base_provider import BaseIntegrationProvider
from src.service.integrations.providers.google_provider import GoogleProvider


class ProviderFactory:
    """
    Factory to retrieve the correct integration provider based on the type.
    """

    _providers: dict[IntegrationType, type[BaseIntegrationProvider]] = {
        IntegrationType.GMAIL: GoogleProvider,
        IntegrationType.GOOGLE_CALENDAR: GoogleProvider,
        # Future integrations map here:
        # IntegrationType.HUBSPOT: HubSpotProvider,
        # IntegrationType.MICROSOFT_TEAMS: MicrosoftTeamsProvider,
        # IntegrationType.GONG: GongProvider,
    }

    @classmethod
    def get_provider(cls, integration_type: IntegrationType) -> BaseIntegrationProvider:
        """
        Retrieves the provider instance for a given IntegrationType.
        
        Args:
            integration_type: The enum representing the provider (e.g. google_mail)
            
        Returns:
            An instantiated subclass of BaseIntegrationProvider
            
        Raises:
            ValueError: If the integration type is not supported yet by our custom flow.
        """
        provider_class = cls._providers.get(integration_type)
        if not provider_class:
            raise ValueError(f"No custom integration provider found for type: {integration_type}")

        return provider_class(integration_type)
