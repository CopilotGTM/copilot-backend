from __future__ import annotations

CONTACT_PRIVACY_DELETION = "contact.privacyDeletion"
CONTACT_CREATION = "contact.creation"
CONTACT_PROPERTY_CHANGE = "contact.propertyChange"
CONTACT_DELETION = "contact.deletion"

CONTACT_PROPERTY_CHANGE_PROPERTIES: tuple[str, ...] = (
    "firstname",
    "lastname",
    "jobtitle",
    "email",
    "hs_additional_emails",
    "phone",
    "mobilephone",
    "fax",
    "website",
    "company",
    "associatedcompanyid",
    "hubspot_owner_id",
    "lifecyclestage",
    "hs_lead_status",
    "hs_marketable_status",
    "address",
    "city",
    "state",
    "zip",
    "country",
    "hs_timezone",
    "notes_last_contacted",
    "notes_last_updated",
)
CONTACT_PROPERTY_CHANGE_PROPERTY_SET = frozenset(CONTACT_PROPERTY_CHANGE_PROPERTIES)

CONTACT_SUBSCRIPTIONS = (
    CONTACT_PRIVACY_DELETION,
    CONTACT_CREATION,
    CONTACT_PROPERTY_CHANGE,
    CONTACT_DELETION,
)
