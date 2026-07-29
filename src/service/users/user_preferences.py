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

DEFAULT_ACCOUNT_LIST_COLUMNS = [
    "name",
    "domain",
    "industry",
    "lifecycle_stage",
    "owner.name",
    "contact_count",
    "deal_count",
    "pipeline_value",
    "number_of_employees",
    "annual_revenue",
    "country",
    "linkedin_url",
    "created_at",
]

DEFAULT_DEAL_LIST_COLUMNS = [
    "name",
    "company_name",
    "stage",
    "amount",
    "expected_close_date",
    "owner",
    "primary_contact_name",
    "last_activity_at",
]

DEFAULT_TASK_LIST_COLUMNS = [
    "subject",
    "due_date",
    "assigned_to.name",
    "deal.name",
    "priority",
    "status",
    "has_reminder",
]

DEFAULT_NAVIGATION_EXPANDED_SECTIONS = {
    "workspace": True,
    "records": True,
    "analytics": True,
    "settings_workspace_setup": True,
    "settings_data": True,
    "settings_automation": True,
    "settings_library": True,
    "settings_account": True,
    "settings_crm_fields": False,
}

SETTINGS_ITEMS = {
    "integrations",
    "forms",
    "crm_fields",
    "pipelines",
    "import_data",
    "agent_actions",
    "reminders",
    "shared_chats",
    "audio",
    "password",
}

CRM_LIST_SORT_FIELDS = {
    "contacts": {"full_name", "owner", "last_contacted_at", "last_activity_at"},
    "accounts": {
        "name",
        "lifecycle_stage",
        "owner",
        "number_of_employees",
        "annual_revenue",
        "created_at",
    },
    "deals": {
        "name",
        "stage",
        "amount",
        "expected_close_date",
        "owner",
        "last_activity_at",
    },
    "tasks": {"subject", "due_date", "priority", "status", "assignee", "created_at"},
}


def _normalize_preferred_deal_currency(currency: Any) -> str | None:
    if not isinstance(currency, str):
        return None
    normalized = currency.strip().upper()
    return normalized if len(normalized) == 3 and normalized.isalpha() else None


def set_preferred_deal_currency(
    preferences: dict[str, Any] | None,
    currency: str | None,
) -> dict[str, Any]:
    """Return preferences with a normalized deal currency default stored."""
    normalized = _normalize_preferred_deal_currency(currency)
    root = dict(preferences or {})
    if normalized is None:
        return root

    crm = dict(root.get("crm") or {})
    deals = dict(crm.get("deals") or {})
    deals["preferred_currency"] = normalized
    crm["deals"] = deals
    root["crm"] = crm
    return root


def normalize_navigation_preferences(preferences: Any) -> dict[str, Any]:
    """Fill navigation defaults while preserving every valid user selection."""
    branch = preferences if isinstance(preferences, dict) else {}
    raw_sections = branch.get("expanded_sections")
    raw_sections = raw_sections if isinstance(raw_sections, dict) else {}
    expanded_sections = {
        key: raw_sections.get(key) if isinstance(raw_sections.get(key), bool) else default
        for key, default in DEFAULT_NAVIGATION_EXPANDED_SECTIONS.items()
    }
    last_settings_item = branch.get("last_settings_item")
    crm_fields_entity = branch.get("crm_fields_entity")
    return {
        "expanded_sections": expanded_sections,
        "last_settings_item": (
            last_settings_item
            if isinstance(last_settings_item, str) and last_settings_item in SETTINGS_ITEMS
            else "integrations"
        ),
        "crm_fields_entity": (
            crm_fields_entity
            if isinstance(crm_fields_entity, str) and crm_fields_entity in {"lead", "deal"}
            else "deal"
        ),
    }


def merge_navigation_preferences(
    preferences: dict[str, Any] | None,
    navigation: dict[str, Any],
) -> dict[str, Any]:
    """Replace the navigation branch without disturbing unrelated preferences."""
    root = dict(preferences or {})
    root["navigation"] = normalize_navigation_preferences(navigation)
    return root


def normalize_crm_list_sorting(preferences: dict[str, Any], entity: str) -> None:
    """Remove malformed saved sorting from one CRM list preference branch."""
    allowed_fields = CRM_LIST_SORT_FIELDS[entity]
    sort_by = preferences.get("sort_by")
    sort_order = preferences.get("sort_order")
    if sort_by in allowed_fields and sort_order in {"asc", "desc"}:
        return
    preferences.pop("sort_by", None)
    preferences.pop("sort_order", None)


def merge_crm_list_sorting(
    preferences: dict[str, Any] | None,
    entity: str,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    """Save one CRM list's sorting without replacing sibling user preferences."""
    root = dict(preferences or {})
    crm = dict(root.get("crm") or {})
    entity_preferences = dict(crm.get(entity) or {})
    list_preferences = dict(entity_preferences.get("list") or {})
    list_preferences.update(sort_by=sort_by, sort_order=sort_order)
    entity_preferences["list"] = list_preferences
    crm[entity] = entity_preferences
    root["crm"] = crm
    return root


def preferred_calendar_sender(preferences: dict[str, Any] | None) -> str | None:
    """Return the normalized preferred calendar address from user preferences."""
    root = preferences if isinstance(preferences, dict) else {}
    communications = root.get("communications")
    communications = communications if isinstance(communications, dict) else {}
    calendar = communications.get("calendar")
    calendar = calendar if isinstance(calendar, dict) else {}
    raw_email = calendar.get("preferred_sender_email")
    if not isinstance(raw_email, str):
        return None
    normalized = raw_email.strip().lower()
    return normalized or None


def merge_preferred_calendar_sender(
    preferences: dict[str, Any] | None,
    sender_email: str | None,
) -> dict[str, Any]:
    """Return preferences with one calendar default updated or cleared."""
    root = dict(preferences or {})
    communications = dict(root.get("communications") or {})
    calendar = dict(communications.get("calendar") or {})
    normalized = (sender_email or "").strip().lower()
    if normalized:
        calendar["preferred_sender_email"] = normalized
    else:
        calendar.pop("preferred_sender_email", None)
    communications["calendar"] = calendar
    root["communications"] = communications
    return root


def normalize_user_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Return user UI preferences with backend-owned defaults filled in."""
    root = dict(preferences or {})
    crm = dict(root.get("crm") or {})
    contacts = dict(crm.get("contacts") or {})
    contact_list = dict(contacts.get("list") or {})
    columns = contact_list.get("columns")

    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        contact_list["columns"] = list(DEFAULT_CONTACT_LIST_COLUMNS)
    normalize_crm_list_sorting(contact_list, "contacts")

    contacts["list"] = contact_list
    crm["contacts"] = contacts

    accounts = dict(crm.get("accounts") or {})
    account_list = dict(accounts.get("list") or {})
    account_columns = account_list.get("columns")

    if not isinstance(account_columns, list) or not all(
        isinstance(column, str) for column in account_columns
    ):
        account_list["columns"] = list(DEFAULT_ACCOUNT_LIST_COLUMNS)
    normalize_crm_list_sorting(account_list, "accounts")

    accounts["list"] = account_list
    crm["accounts"] = accounts

    deals = dict(crm.get("deals") or {})
    deal_list = dict(deals.get("list") or {})
    deal_columns = deal_list.get("columns")

    if not isinstance(deal_columns, list) or not all(
        isinstance(column, str) for column in deal_columns
    ):
        deal_list["columns"] = list(DEFAULT_DEAL_LIST_COLUMNS)
    normalize_crm_list_sorting(deal_list, "deals")

    preferred_currency = _normalize_preferred_deal_currency(deals.get("preferred_currency"))
    if preferred_currency:
        deals["preferred_currency"] = preferred_currency
    else:
        deals.pop("preferred_currency", None)

    deals["list"] = deal_list
    crm["deals"] = deals

    tasks = dict(crm.get("tasks") or {})
    task_list = dict(tasks.get("list") or {})
    task_columns = task_list.get("columns")

    if not isinstance(task_columns, list) or not all(
        isinstance(column, str) for column in task_columns
    ):
        task_list["columns"] = list(DEFAULT_TASK_LIST_COLUMNS)
    normalize_crm_list_sorting(task_list, "tasks")

    tasks["list"] = task_list
    crm["tasks"] = tasks
    root["crm"] = crm
    root["navigation"] = normalize_navigation_preferences(root.get("navigation"))
    calendar_sender = preferred_calendar_sender(root)
    if calendar_sender:
        root = merge_preferred_calendar_sender(root, calendar_sender)
    return root
