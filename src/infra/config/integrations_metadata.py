"""
Integration metadata configuration.

This file contains static metadata for all supported integrations.
Update this file when adding new integration types or changing display information.
"""

from dataclasses import dataclass
from typing import Dict, Optional, List
from ...domain.models.organisation_tables.knowledge_models import IntegrationType


# Category display names
class IntegrationCategory:
    """Constants for integration category display names."""

    CRM_SALES = "CRM & Sales"
    COLLABORATION = "Collaboration & Communication"
    STORAGE = "Cloud Storage"
    PROJECT_MANAGEMENT = "Project Management"
    CUSTOMER_SUPPORT = "Customer Support"
    SALES_ENABLEMENT = "Sales Enablement"
    SCHEDULING = "Scheduling & Calendar"


# Availability status
class AvailabilityStatus:
    """Constants for integration availability status."""

    AVAILABLE = "available"
    COMING_SOON = "coming_soon"
    BETA = "beta"
    DEPRECATED = "deprecated"


# Access level
class IntegrationAccessLevel:
    """Constants for integration access level."""

    USER = "user"  # Available to all users in the organization
    ADMIN = "admin"  # Available only to organization admins


@dataclass(frozen=True)
class IntegrationMetadata:
    """
    Immutable metadata for an integration type.

    Attributes:
        name: Display name shown to users
        description: Brief description of what the integration does
        logo_url: URL to the integration provider's logo
        category: Category slug for grouping (crm, collaboration, storage, etc.)
        category_name: Human-readable category name for display
        availability_status: Integration availability (available, coming_soon, beta, deprecated)
        access_level: Who can configure this integration (user, admin)
        oauth_scopes: Default OAuth scopes required for this integration
        documentation_url: Link to integration documentation (optional)
        default_sync_frequency: Recommended sync frequency
        supported_sync_frequencies: List of allowed sync frequencies
    """

    name: str
    description: str
    logo_url: str
    category: str
    category_name: str
    availability_status: str = AvailabilityStatus.AVAILABLE
    access_level: str = IntegrationAccessLevel.USER
    oauth_scopes: Optional[List[str]] = None
    documentation_url: Optional[str] = None
    default_sync_frequency: str = "daily"
    supported_sync_frequencies: Optional[List[str]] = None

    def __post_init__(self):
        # Set default supported frequencies if not provided
        if self.supported_sync_frequencies is None:
            object.__setattr__(
                self,
                "supported_sync_frequencies",
                ["hourly", "daily", "weekly", "manual"],
            )


# Integration metadata registry
INTEGRATION_METADATA: Dict[IntegrationType, IntegrationMetadata] = {
    IntegrationType.HUBSPOT: IntegrationMetadata(
        name="HubSpot",
        description="Sync contacts, deals, and sales activities from HubSpot CRM",
        logo_url="https://www.hubspot.com/hubfs/HubSpot_Logos/HubSpot-Inversed-Favicon.png",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "crm.objects.contacts.read",
            "crm.objects.deals.read",
            "crm.objects.companies.read",
            "files",
        ],
        documentation_url="https://developers.hubspot.com/docs/api/overview",
        default_sync_frequency="daily",
    ),
    IntegrationType.SLACK: IntegrationMetadata(
        name="Slack",
        description="Sync messages, files, and channel data from Slack workspaces",
        logo_url="https://a.slack-edge.com/80588/marketing/img/icons/icon_slack_hash_colored.png",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "channels:read",
            "channels:history",
            "files:read",
            "users:read",
        ],
        documentation_url="https://api.slack.com/docs",
        default_sync_frequency="hourly",
    ),
    IntegrationType.GOOGLE_DRIVE: IntegrationMetadata(
        name="Google Drive",
        description="Sync documents, spreadsheets, and files from Google Drive",
        logo_url="https://ssl.gstatic.com/images/branding/product/2x/drive_2020q4_48dp.png",
        category="storage",
        category_name=IntegrationCategory.STORAGE,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        documentation_url="https://developers.google.com/drive/api/guides/about-sdk",
        default_sync_frequency="daily",
    ),
    IntegrationType.GOOGLE_CALENDAR: IntegrationMetadata(
        name="Google Calendar",
        description="Sync events and availability from Google Calendar",
        logo_url="https://cdn.simpleicons.org/googlecalendar",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            # Consider adding Gmail profile or directory scopes only if needed later
        ],
        documentation_url="https://developers.google.com/calendar/api",
        default_sync_frequency="hourly",
    ),
    IntegrationType.OUTLOOK_CALENDAR: IntegrationMetadata(
        name="Outlook Calendar",
        description="Sync events and availability from Microsoft Outlook/Office 365",
        logo_url="https://static2.sharepointonline.com/files/fabric/assets/brand-icons/product/svg/outlook_48x1.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,
        oauth_scopes=[
            # Minimal read access for Microsoft Graph
            "Calendars.Read",
        ],
        documentation_url="https://learn.microsoft.com/en-us/graph/api/resources/calendar?view=graph-rest-1.0",
        default_sync_frequency="hourly",
    ),
    IntegrationType.CALENDLY: IntegrationMetadata(
        name="Calendly",
        description="Ingest scheduled meetings and invitee details from Calendly",
        logo_url="https://cdn.simpleicons.org/calendly",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,
        oauth_scopes=None,  # token-based OAuth; set when implementing Nango provider
        documentation_url="https://developer.calendly.com/",
        default_sync_frequency="hourly",
    ),
    IntegrationType.SALESFORCE: IntegrationMetadata(
        name="Salesforce",
        description="Sync leads, opportunities, and sales data from Salesforce CRM",
        logo_url="https://www.salesforce.com/content/dam/sfdc-docs/www/logos/logo-salesforce.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        documentation_url="https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        default_sync_frequency="daily",
    ),
    IntegrationType.NOTION: IntegrationMetadata(
        name="Notion",
        description="Sync pages, databases, and documentation from Notion workspaces",
        logo_url="https://www.notion.so/images/favicon.ico",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        documentation_url="https://developers.notion.com/",
        default_sync_frequency="daily",
    ),
    IntegrationType.CONFLUENCE: IntegrationMetadata(
        name="Confluence",
        description="Sync pages and documentation from Atlassian Confluence",
        logo_url="https://cdn.iconscout.com/icon/free/png-256/free-confluence-logo-icon-download-in-svg-png-gif-file-formats--technology-social-media-company-brand-vol-5-pack-logos-icons-2945073.png",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        documentation_url="https://developer.atlassian.com/cloud/confluence/rest/v1/intro/",
        default_sync_frequency="daily",
    ),
    IntegrationType.ONEDRIVE: IntegrationMetadata(
        name="OneDrive",
        description="Sync documents and files from Microsoft OneDrive",
        logo_url="https://upload.wikimedia.org/wikipedia/commons/3/3c/Microsoft_Office_OneDrive_%282019%E2%80%93present%29.svg",
        category="storage",
        category_name=IntegrationCategory.STORAGE,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        access_level=IntegrationAccessLevel.USER,  # User level for now
        documentation_url="https://learn.microsoft.com/en-us/onedrive/developer/",
        default_sync_frequency="daily",
    ),
}


def get_integration_metadata(
    integration_type: IntegrationType,
) -> Optional[IntegrationMetadata]:
    """
    Get metadata for a specific integration type.

    Args:
        integration_type: The integration type to look up

    Returns:
        IntegrationMetadata if found, None otherwise
    """
    return INTEGRATION_METADATA.get(integration_type)


def get_all_integration_metadata() -> Dict[IntegrationType, IntegrationMetadata]:
    """
    Get all integration metadata.

    Returns:
        Dictionary mapping integration types to their metadata
    """
    return INTEGRATION_METADATA.copy()


def list_integrations_by_category(
    category: str,
) -> List[tuple[IntegrationType, IntegrationMetadata]]:
    """
    Get all integrations in a specific category.

    Args:
        category: Category to filter by (crm, collaboration, storage, etc.)

    Returns:
        List of (integration_type, metadata) tuples
    """
    return [
        (int_type, metadata)
        for int_type, metadata in INTEGRATION_METADATA.items()
        if metadata.category == category
    ]


def get_all_categories() -> Dict[str, str]:
    """
    Get all unique categories with their display names.

    Returns:
        Dictionary mapping category slug to category display name
        Example: {"crm": "CRM & Sales", "collaboration": "Collaboration & Communication"}
    """
    categories = {}
    for metadata in INTEGRATION_METADATA.values():
        if metadata.category not in categories:
            categories[metadata.category] = metadata.category_name
    return categories
