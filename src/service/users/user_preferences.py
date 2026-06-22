"""User preference defaults shared by login and session bootstrap responses."""

from __future__ import annotations

from typing import Any

DEFAULT_CONTACT_LIST_COLUMNS = [
    "full_name",
    "job_title",
    "company.name",
    "email",
    "phone",
    "linkedin_url",
    "owner.name",
    "deal_count",
    "last_contacted_at",
    "last_activity_at",
]


def normalize_user_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Return user UI preferences with backend-owned defaults filled in."""
    root = dict(preferences or {})
    crm = dict(root.get("crm") or {})
    contacts = dict(crm.get("contacts") or {})
    contact_list = dict(contacts.get("list") or {})
    columns = contact_list.get("columns")

    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        contact_list["columns"] = list(DEFAULT_CONTACT_LIST_COLUMNS)

    contacts["list"] = contact_list
    crm["contacts"] = contacts
    root["crm"] = crm
    return root
