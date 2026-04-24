"""
Integration metadata configuration.

This file contains static metadata for all supported integrations.
Update this file when adding new integration types or changing display information.
"""

from dataclasses import dataclass

from ...domain.models.master_tables import UserAppRole
from ...domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
)


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
        default_scope: Default access scope (ORGANIZATION or USER)
        can_be_created_by: List of user roles that can create this integration
        allow_multiple_connections: Whether multiple instances can be connected per org
    """

    name: str
    description: str
    logo_url: str
    category: str
    category_name: str
    availability_status: str = AvailabilityStatus.AVAILABLE
    access_level: str = IntegrationAccessLevel.USER
    oauth_scopes: list[str] | None = None
    documentation_url: str | None = None
    default_sync_frequency: str = "daily"
    supported_sync_frequencies: list[str] | None = None
    # NEW: Access control configuration
    default_scope: IntegrationScope = IntegrationScope.ORGANIZATION
    can_be_created_by: list[UserAppRole] | None = None
    allow_multiple_connections: bool = False

    def __post_init__(self):
        # Set default supported frequencies if not provided
        if self.supported_sync_frequencies is None:
            object.__setattr__(
                self,
                "supported_sync_frequencies",
                ["hourly", "daily", "weekly", "manual"],
            )

        # Set default roles if not provided
        if self.can_be_created_by is None:
            # Default: Only admins can create org-wide integrations
            if self.default_scope == IntegrationScope.ORGANIZATION:
                object.__setattr__(
                    self,
                    "can_be_created_by",
                    [UserAppRole.ADMIN],
                )
            else:
                # User-scoped integrations can be created by anyone
                object.__setattr__(
                    self,
                    "can_be_created_by",
                    [UserAppRole.MEMBER, UserAppRole.ADMIN],
                )

    def can_user_create(self, user_role: UserAppRole) -> bool:
        """Check if a user with the given role can create this integration."""
        return user_role in (self.can_be_created_by or [])


# Integration metadata registry
INTEGRATION_METADATA: dict[IntegrationType, IntegrationMetadata] = {
    IntegrationType.HUBSPOT: IntegrationMetadata(
        name="HubSpot",
        description="Connect HubSpot using Copilot's custom OAuth flow. CRM sync setup follows separately.",
        logo_url="https://api.nango.dev/images/template-logos/hubspot.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=[
            "oauth",
            "automation",
            "conversations.read",
            "sales-email-read",
            "crm.objects.companies.read",
            "crm.objects.companies.write",
            "crm.objects.contacts.read",
            "crm.objects.contacts.write",
            "crm.objects.deals.read",
            "crm.objects.deals.write",
            "crm.objects.leads.read",
            "crm.objects.leads.write",
            "crm.objects.line_items.read",
            "crm.objects.line_items.write",
            "crm.objects.products.read",
            "crm.objects.products.write",
            "crm.objects.owners.read",
            "crm.objects.users.read",
            "crm.schemas.companies.read",
            "crm.schemas.contacts.read",
            "crm.schemas.deals.read",
        ],
        documentation_url="https://developers.hubspot.com/docs/api/overview",
        default_sync_frequency="manual",
        # Access Control
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
    IntegrationType.INTERNAL_CRM: IntegrationMetadata(
        name="Copilot CRM",
        description="Manage deals, contacts, and pipelines directly in Copilot",
        logo_url="https://assets.copilotgtm.com/integrations/copilot-crm.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.AVAILABLE,
        access_level=IntegrationAccessLevel.ADMIN,
        documentation_url=None,
        default_sync_frequency="manual",
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN],
        allow_multiple_connections=False,
    ),
    IntegrationType.SLACK: IntegrationMetadata(
        name="Slack",
        description="Sync messages, files, and channel data from Slack workspaces",
        logo_url="https://api.nango.dev/images/template-logos/slack.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "channels:read",
            "groups:read",
            "mpim:read",
            "channels:history",
            "groups:history",
            "mpim:history",
            "users:read",
            "users:read.email",
            "team:read",
        ],
        documentation_url="https://api.slack.com/docs",
        default_sync_frequency="hourly",
        # Each user connects their own Slack account
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.TEAMS: IntegrationMetadata(
        name="Microsoft Teams",
        description="Sync messages, channels, and files from Microsoft Teams",
        logo_url="https://api.nango.dev/images/template-logos/microsoft-teams.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
            "Files.Read.All",
            "User.Read.All",
        ],
        documentation_url="https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview",
        default_sync_frequency="hourly",
        # Each user connects their own Teams account
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.GMAIL: IntegrationMetadata(
        name="Gmail",
        description="Sync emails, threads, and folders from Gmail",
        logo_url="https://app.nango.dev/images/template-logos/google-mail.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.labels",
        ],
        documentation_url="https://developers.google.com/gmail/api/guides",
        default_sync_frequency="hourly",
        # Access Control - User level for personal email
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.OUTLOOK_EMAIL: IntegrationMetadata(
        name="Outlook (Email)",
        description="Sync emails, threads, and folders from Outlook/Office 365 mailbox",
        logo_url="https://app.nango.dev/images/template-logos/outlook.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "Mail.Read",
            "MailboxSettings.Read",
        ],
        documentation_url="https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview",
        default_sync_frequency="hourly",
        # Access Control - User level for personal email
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.GOOGLE_DRIVE: IntegrationMetadata(
        name="Google Drive",
        description="Sync documents, spreadsheets, and files from Google Drive",
        logo_url="https://api.nango.dev/images/template-logos/google-drive.svg",
        category="storage",
        category_name=IntegrationCategory.STORAGE,
        availability_status=AvailabilityStatus.COMING_SOON,  # Coming soon
        documentation_url="https://developers.google.com/drive/api/guides/about-sdk",
        default_sync_frequency="daily",
        # Access Control - User level for personal files
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.GOOGLE_CALENDAR: IntegrationMetadata(
        name="Google Calendar",
        description="Sync events and availability from Google Calendar",
        logo_url="https://api.nango.dev/images/template-logos/google-calendar.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        ],
        documentation_url="https://developers.google.com/calendar/api",
        default_sync_frequency="hourly",
        # Access Control - User level for personal calendars
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.OUTLOOK_CALENDAR: IntegrationMetadata(
        name="Outlook Calendar",
        description="Sync calendar events and availability from Microsoft Outlook/Office 365",
        logo_url="https://app.nango.dev/images/template-logos/outlook.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.AVAILABLE,
        oauth_scopes=[
            "Calendars.Read",
        ],
        documentation_url="https://learn.microsoft.com/en-us/graph/api/resources/calendar?view=graph-rest-1.0",
        default_sync_frequency="hourly",
        # Access Control - User level for personal calendars
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.CALENDLY: IntegrationMetadata(
        name="Calendly",
        description="Ingest scheduled meetings and invitee details from Calendly",
        logo_url="https://api.nango.dev/images/template-logos/calendly.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,
        oauth_scopes=None,
        documentation_url="https://developer.calendly.com/",
        default_sync_frequency="hourly",
        # Access Control - User level for personal Calendly
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
    IntegrationType.SALESFORCE: IntegrationMetadata(
        name="Salesforce",
        description="Connect Salesforce using Copilot's custom OAuth flow. CRM sync setup follows separately.",
        logo_url="https://api.nango.dev/images/template-logos/salesforce.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=["refresh_token", "api", "openid", "profile", "email"],
        documentation_url="https://developer.salesforce.com/docs/platform/mobile-sdk/guide/oauth-scope-parameter-values.html",
        default_sync_frequency="manual",
        # Access Control - Organization wide CRM
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
    IntegrationType.GONG: IntegrationMetadata(
        name="Gong",
        description="Ingest call recordings, transcripts, and deal insights from Gong",
        logo_url="https://api.nango.dev/images/template-logos/gong-oauth.svg",
        category="sales_enablement",
        category_name=IntegrationCategory.SALES_ENABLEMENT,
        availability_status=AvailabilityStatus.COMING_SOON,
        documentation_url="https://docs.gong.io/docs",
        default_sync_frequency="daily",
        # Access Control - Conversation intelligence data is organisation-scoped
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
    IntegrationType.NOTION: IntegrationMetadata(
        name="Notion",
        description="Sync pages, databases, and documentation from Notion workspaces",
        logo_url="https://api.nango.dev/images/template-logos/notion.svg",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,
        documentation_url="https://developers.notion.com/",
        default_sync_frequency="daily",
        # Access Control - Organization workspace
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
    IntegrationType.CONFLUENCE: IntegrationMetadata(
        name="Confluence",
        description="Sync pages and documentation from Atlassian Confluence",
        logo_url="https://cdn.simpleicons.org/confluence",
        category="collaboration",
        category_name=IntegrationCategory.COLLABORATION,
        availability_status=AvailabilityStatus.COMING_SOON,
        documentation_url="https://developer.atlassian.com/cloud/confluence/rest/v1/intro/",
        default_sync_frequency="daily",
        # Access Control - Organization workspace
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
    IntegrationType.ONEDRIVE: IntegrationMetadata(
        name="OneDrive",
        description="Sync documents and files from Microsoft OneDrive",
        logo_url="https://api.nango.dev/images/template-logos/one-drive.svg",
        category="storage",
        category_name=IntegrationCategory.STORAGE,
        availability_status=AvailabilityStatus.COMING_SOON,
        access_level=IntegrationAccessLevel.USER,
        documentation_url="https://learn.microsoft.com/en-us/onedrive/developer/",
        default_sync_frequency="daily",
        # Access Control - User level for personal OneDrive
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
}


def get_integration_metadata(
    integration_type: IntegrationType,
) -> IntegrationMetadata | None:
    """
    Get metadata for a specific integration type.

    Args:
        integration_type: The integration type to look up

    Returns:
        IntegrationMetadata if found, None otherwise
    """
    return INTEGRATION_METADATA.get(integration_type)


def get_all_integration_metadata() -> dict[IntegrationType, IntegrationMetadata]:
    """
    Get all integration metadata.

    Returns:
        Dictionary mapping integration types to their metadata
    """
    return INTEGRATION_METADATA.copy()


def list_integrations_by_category(
    category: str,
) -> list[tuple[IntegrationType, IntegrationMetadata]]:
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


def get_all_categories() -> dict[str, str]:
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
