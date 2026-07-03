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

    accounts = dict(crm.get("accounts") or {})
    account_list = dict(accounts.get("list") or {})
    account_columns = account_list.get("columns")

    if not isinstance(account_columns, list) or not all(
        isinstance(column, str) for column in account_columns
    ):
        account_list["columns"] = list(DEFAULT_ACCOUNT_LIST_COLUMNS)

    accounts["list"] = account_list
    crm["accounts"] = accounts

    deals = dict(crm.get("deals") or {})
    deal_list = dict(deals.get("list") or {})
    deal_columns = deal_list.get("columns")

    if not isinstance(deal_columns, list) or not all(
        isinstance(column, str) for column in deal_columns
    ):
        deal_list["columns"] = list(DEFAULT_DEAL_LIST_COLUMNS)

    deals["list"] = deal_list
    crm["deals"] = deals

    tasks = dict(crm.get("tasks") or {})
    task_list = dict(tasks.get("list") or {})
    task_columns = task_list.get("columns")

    if not isinstance(task_columns, list) or not all(
        isinstance(column, str) for column in task_columns
    ):
        task_list["columns"] = list(DEFAULT_TASK_LIST_COLUMNS)

    tasks["list"] = task_list
    crm["tasks"] = tasks
    root["crm"] = crm
    return root
