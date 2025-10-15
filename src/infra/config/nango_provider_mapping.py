"""
Utilities to map between internal IntegrationType values and Nango provider_config_key.

Reason: Our enum uses underscores (e.g., "google_calendar") while Nango provider keys
often use hyphens (e.g., "google-calendar"). HubSpot/Slack work because the keys match.
"""

from __future__ import annotations

from typing import Dict

from src.domain.models.organisation_tables.knowledge_models import IntegrationType


# Explicit mappings where Nango's provider key differs from our enum value
_INTEGRATION_TO_NANGO: Dict[IntegrationType, str] = {
    # Collaboration & Communication
    IntegrationType.SLACK: "slack",
    IntegrationType.TEAMS: "teams",
    IntegrationType.GOOGLE_CHAT: "google-chat",
    IntegrationType.ZOOM: "zoom",
    IntegrationType.GOOGLE_DRIVE: "google-drive",
    IntegrationType.GOOGLE_CALENDAR: "google-calendar",
    IntegrationType.OUTLOOK_CALENDAR: "outlook-calendar",
    IntegrationType.SHAREPOINT: "sharepoint",
    IntegrationType.ONEDRIVE: "onedrive",
    IntegrationType.BOX: "box",
    IntegrationType.DROPBOX: "dropbox",
    IntegrationType.NOTION: "notion",
    IntegrationType.CONFLUENCE: "confluence",
    IntegrationType.MIRO: "miro",
    IntegrationType.FIGMA: "figma",
    IntegrationType.CALENDLY: "calendly",
    # Project Management
    IntegrationType.JIRA: "jira",
    IntegrationType.TRELLO: "trello",
    IntegrationType.ASANA: "asana",
    IntegrationType.MONDAY: "monday",
    # CRM & Customer Support
    IntegrationType.ZENDESK: "zendesk",
    IntegrationType.INTERCOM: "intercom",
    IntegrationType.PIPEDRIVE: "pipedrive",
    IntegrationType.ZOHO_CRM: "zoho-crm",
    # Sales & Presales Enablement
    IntegrationType.SALESFORCE: "salesforce",
    IntegrationType.HUBSPOT: "hubspot",
    IntegrationType.SEISMIC: "seismic",
    IntegrationType.HIGHSPOT: "highspot",
    IntegrationType.SHOWPAD: "showpad",
    IntegrationType.OUTREACH: "outreach",
    IntegrationType.SALESLOFT: "salesloft",
    IntegrationType.GONG: "gong",
    IntegrationType.CHORUS: "chorus",
}


def _build_reverse_map(
    mapping: Dict[IntegrationType, str],
) -> Dict[str, IntegrationType]:
    return {v.lower(): k for k, v in mapping.items()}


# Build reverse maps (lower-cased)
_NANGO_TO_INTEGRATION: Dict[str, IntegrationType] = _build_reverse_map(
    _INTEGRATION_TO_NANGO
)

# Overrides for Nango Connect Session integration keys.
# In many cases the integration unique key matches the provider key (e.g., "slack").
# When it differs, list the unique key here so we can target the correct integration.
_NANGO_CONNECT_KEYS: Dict[IntegrationType, str] = {
    # Collaboration & Communication
    IntegrationType.SLACK: "slack",
    IntegrationType.TEAMS: "teams",
    IntegrationType.GOOGLE_CHAT: "google-chat",
    IntegrationType.ZOOM: "zoom",
    IntegrationType.GOOGLE_DRIVE: "google-drive",
    IntegrationType.GOOGLE_CALENDAR: "google-calendar-getting-started",
    IntegrationType.OUTLOOK_CALENDAR: "outlook-calendar",
    IntegrationType.SHAREPOINT: "sharepoint",
    IntegrationType.ONEDRIVE: "onedrive",
    IntegrationType.BOX: "box",
    IntegrationType.DROPBOX: "dropbox",
    IntegrationType.NOTION: "notion",
    IntegrationType.CONFLUENCE: "confluence",
    IntegrationType.MIRO: "miro",
    IntegrationType.FIGMA: "figma",
    IntegrationType.CALENDLY: "calendly",
    # Project Management
    IntegrationType.JIRA: "jira",
    IntegrationType.TRELLO: "trello",
    IntegrationType.ASANA: "asana",
    IntegrationType.MONDAY: "monday",
    # CRM & Customer Support
    IntegrationType.ZENDESK: "zendesk",
    IntegrationType.INTERCOM: "intercom",
    IntegrationType.PIPEDRIVE: "pipedrive",
    IntegrationType.ZOHO_CRM: "zoho-crm",
    # Sales & Presales Enablement
    IntegrationType.SALESFORCE: "salesforce",
    IntegrationType.HUBSPOT: "hubspot",
    IntegrationType.SEISMIC: "seismic",
    IntegrationType.HIGHSPOT: "highspot",
    IntegrationType.SHOWPAD: "showpad",
    IntegrationType.OUTREACH: "outreach",
    IntegrationType.SALESLOFT: "salesloft",
    IntegrationType.GONG: "gong",
    IntegrationType.CHORUS: "chorus",
}

_NANGO_CONNECT_TO_INTEGRATION: Dict[str, IntegrationType] = _build_reverse_map(
    _NANGO_CONNECT_KEYS
)


def to_nango_provider_key(integration_type: IntegrationType) -> str:
    """Return the Nango provider_config_key for an IntegrationType.

    Falls back to the enum value if no override exists.
    """
    fallback_key = integration_type.value.replace("_", "-")
    return _INTEGRATION_TO_NANGO.get(integration_type, fallback_key)


def to_nango_connect_integration_key(integration_type: IntegrationType) -> str:
    """Return the Nango Connect Session integration key for an IntegrationType."""
    return _NANGO_CONNECT_KEYS.get(
        integration_type,
        to_nango_provider_key(integration_type),
    )


def from_nango_provider_key(provider_key: str) -> IntegrationType:
    """Map a Nango provider_config_key back to our IntegrationType.

    Tries explicit reverse maps first (provider and connect keys). If not found,
    progressively trims hyphen suffixes (e.g., "google-calendar-getting-started"
    -> "google-calendar"). Finally, replaces hyphens with underscores and tries
    to construct the enum (e.g., "google-calendar" -> "google_calendar").
    """
    key = (provider_key or "").lower().strip()

    # Direct matches
    if key in _NANGO_TO_INTEGRATION:
        return _NANGO_TO_INTEGRATION[key]
    if key in _NANGO_CONNECT_TO_INTEGRATION:
        return _NANGO_CONNECT_TO_INTEGRATION[key]

    # Progressive hyphen-trim
    candidate = key
    while True:
        if candidate in _NANGO_TO_INTEGRATION:
            return _NANGO_TO_INTEGRATION[candidate]
        if candidate in _NANGO_CONNECT_TO_INTEGRATION:
            return _NANGO_CONNECT_TO_INTEGRATION[candidate]
        if "-" not in candidate:
            break
        candidate = candidate.rsplit("-", 1)[0]

    # Enum fallback
    normalized = key.replace("-", "_")
    return IntegrationType(normalized)
