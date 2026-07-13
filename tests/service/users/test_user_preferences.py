from src.service.users.user_preferences import (
    normalize_user_preferences,
    set_preferred_deal_currency,
)


def test_normalize_user_preferences_adds_default_contact_list_columns() -> None:
    assert normalize_user_preferences({})["crm"]["contacts"]["list"]["columns"] == [
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


def test_normalize_user_preferences_adds_default_account_list_columns() -> None:
    assert normalize_user_preferences({})["crm"]["accounts"]["list"]["columns"] == [
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


def test_normalize_user_preferences_adds_default_deal_list_columns() -> None:
    assert normalize_user_preferences({})["crm"]["deals"]["list"]["columns"] == [
        "name",
        "company_name",
        "stage",
        "amount",
        "expected_close_date",
        "owner",
        "primary_contact_name",
        "last_activity_at",
    ]


def test_normalize_user_preferences_adds_default_task_list_columns() -> None:
    assert normalize_user_preferences({})["crm"]["tasks"]["list"]["columns"] == [
        "subject",
        "due_date",
        "assigned_to.name",
        "deal.name",
        "priority",
        "status",
        "has_reminder",
    ]


def test_normalize_user_preferences_preserves_user_contact_list_columns() -> None:
    preferences = {
        "crm": {
            "contacts": {
                "list": {
                    "columns": ["full_name", "email", "owner.name"],
                }
            }
        }
    }

    assert normalize_user_preferences(preferences)["crm"]["contacts"]["list"]["columns"] == [
        "full_name",
        "email",
        "owner.name",
    ]


def test_normalize_user_preferences_preserves_user_account_list_columns() -> None:
    preferences = {
        "crm": {
            "accounts": {
                "list": {
                    "columns": ["name", "owner.name", "pipeline_value"],
                }
            }
        }
    }

    assert normalize_user_preferences(preferences)["crm"]["accounts"]["list"]["columns"] == [
        "name",
        "owner.name",
        "pipeline_value",
    ]


def test_normalize_user_preferences_preserves_user_deal_list_columns() -> None:
    preferences = {
        "crm": {
            "deals": {
                "list": {
                    "columns": ["name", "amount", "owner.name"],
                }
            }
        }
    }

    assert normalize_user_preferences(preferences)["crm"]["deals"]["list"]["columns"] == [
        "name",
        "amount",
        "owner.name",
    ]


def test_normalize_user_preferences_normalizes_preferred_deal_currency() -> None:
    preferences = {
        "crm": {
            "deals": {
                "preferred_currency": " inr ",
                "list": {"columns": ["name", "amount"]},
            }
        }
    }

    normalized = normalize_user_preferences(preferences)

    assert normalized["crm"]["deals"]["preferred_currency"] == "INR"
    assert normalized["crm"]["deals"]["list"]["columns"] == ["name", "amount"]


def test_normalize_user_preferences_ignores_invalid_preferred_deal_currency() -> None:
    for value in ["rupee", "12$", 123, None]:
        preferences = {"crm": {"deals": {"preferred_currency": value}}}

        normalized = normalize_user_preferences(preferences)

        assert "preferred_currency" not in normalized["crm"]["deals"]


def test_set_preferred_deal_currency_preserves_unrelated_preferences() -> None:
    preferences = {
        "notifications": {"email": True},
        "crm": {"deals": {"list": {"columns": ["name"]}}},
    }

    updated = set_preferred_deal_currency(preferences, " usd ")

    assert updated["notifications"] == {"email": True}
    assert updated["crm"]["deals"]["list"]["columns"] == ["name"]
    assert updated["crm"]["deals"]["preferred_currency"] == "USD"


def test_set_preferred_deal_currency_ignores_empty_or_invalid_currency() -> None:
    preferences = {"crm": {"deals": {"preferred_currency": "INR"}}}

    assert set_preferred_deal_currency(preferences, None) == preferences
    assert set_preferred_deal_currency(preferences, "") == preferences
    assert set_preferred_deal_currency(preferences, "USDT") == preferences
    assert set_preferred_deal_currency(preferences, "12$") == preferences


def test_normalize_user_preferences_preserves_user_task_list_columns() -> None:
    preferences = {
        "crm": {
            "tasks": {
                "list": {
                    "columns": ["subject", "due_date", "status"],
                }
            }
        }
    }

    assert normalize_user_preferences(preferences)["crm"]["tasks"]["list"]["columns"] == [
        "subject",
        "due_date",
        "status",
    ]
