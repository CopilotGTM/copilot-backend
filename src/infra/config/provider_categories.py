"""Provider categorization and mapping helpers.

Maps IntegrationType to (category, provider) used by sync dispatcher/registry.
"""

from __future__ import annotations

from src.domain.models.organisation_tables.sync import IntegrationType


def categorize_integration_type(int_type: IntegrationType) -> tuple[str, str]:
    """Return (category, provider) for a given IntegrationType.

    category: 'calendar' | 'crm' | 'email' (future)
    provider: short provider key used by registry ('google', 'outlook', 'hubspot', ...)
    """
    # Calendar
    if int_type == IntegrationType.GOOGLE_CALENDAR:
        return ("calendar", "google")
    if int_type == IntegrationType.OUTLOOK_CALENDAR:
        return ("calendar", "outlook")
    # CRM
    if int_type == IntegrationType.HUBSPOT:
        return ("crm", "hubspot")
    if int_type == IntegrationType.SALESFORCE:
        return ("crm", "salesforce")
    if int_type == IntegrationType.ZOHO_CRM:
        return ("crm", "zoho")
    if int_type == IntegrationType.PIPEDRIVE:
        return ("crm", "pipedrive")
    # Conversation intelligence
    if int_type == IntegrationType.GONG:
        return ("conversation_intelligence", "gong")
    if int_type == IntegrationType.CHORUS:
        return ("conversation_intelligence", "chorus")
    # Default bucket
    return ("other", int_type.value)


__all__ = ["categorize_integration_type"]
