"""HubSpot company webhook subscription names supported by this service."""

from __future__ import annotations

COMPANY_CREATION = "company.creation"
COMPANY_PROPERTY_CHANGE = "company.propertyChange"
COMPANY_DELETION = "company.deletion"

COMPANY_SUBSCRIPTIONS = (
    COMPANY_CREATION,
    COMPANY_PROPERTY_CHANGE,
    COMPANY_DELETION,
)

# Product-approved HubSpot company properties for v1 account webhooks. The
# single-company fetch still requests the full mapped snapshot so unselected
# columns do not get nulled during a selected-property refresh.
COMPANY_PROPERTY_CHANGE_PROPERTIES = (
    "domain",
    "description",
    "name",
    "website",
    "industry",
    "type",
    "annualrevenue",
    "phone",
    "address",
    "city",
    "state",
    "country",
    "zip",
)

COMPANY_PROPERTY_CHANGE_PROPERTY_SET = frozenset(COMPANY_PROPERTY_CHANGE_PROPERTIES)
