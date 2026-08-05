"""Tests for normalized defaults stored in the shared user preferences document."""

from src.service.users.user_preferences import (
    merge_crm_list_sorting,
    merge_preferred_calendar_sender,
    merge_skipped_onboarding_step,
    normalize_user_preferences,
    preferred_calendar_sender,
    set_preferred_deal_currency,
)


def test_skipped_onboarding_steps_are_normalized_and_preserve_other_preferences() -> None:
    updated = merge_skipped_onboarding_step(
        {
            "onboarding": {"skipped_steps": ["whatsapp", "unknown"]},
            "notifications": {"email": True},
        },
        "whatsapp",
    )

    assert normalize_user_preferences(updated)["onboarding"] == {"skipped_steps": ["whatsapp"]}
    assert updated["notifications"] == {"email": True}


def test_normalize_user_preferences_adds_default_contact_list_columns() -> None:
    """Missing contact table settings should receive the product defaults."""
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
    """Missing account table settings should receive the product defaults."""
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
        "created_at",
    ]


def test_normalize_user_preferences_adds_default_deal_list_columns() -> None:
    """Missing deal table settings should receive the product defaults."""
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
    """Missing task table settings should receive the product defaults."""
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
    """An explicit contact column selection should not be replaced by defaults."""
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
    """An explicit account column selection should not be replaced by defaults."""
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
    """An explicit deal column selection should not be replaced by defaults."""
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
    """A valid deal currency should be stored in canonical uppercase form."""
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
    """Malformed currency values should not leak into normalized preferences."""
    for value in ["rupee", "12$", 123, None]:
        preferences = {"crm": {"deals": {"preferred_currency": value}}}

        normalized = normalize_user_preferences(preferences)

        assert "preferred_currency" not in normalized["crm"]["deals"]


def test_set_preferred_deal_currency_preserves_unrelated_preferences() -> None:
    """Updating deal currency should preserve all sibling preference branches."""
    preferences = {
        "notifications": {"email": True},
        "crm": {"deals": {"list": {"columns": ["name"]}}},
    }

    updated = set_preferred_deal_currency(preferences, " usd ")

    assert updated["notifications"] == {"email": True}
    assert updated["crm"]["deals"]["list"]["columns"] == ["name"]
    assert updated["crm"]["deals"]["preferred_currency"] == "USD"


def test_set_preferred_deal_currency_ignores_empty_or_invalid_currency() -> None:
    """Invalid currency updates should leave the original document untouched."""
    preferences = {"crm": {"deals": {"preferred_currency": "INR"}}}

    assert set_preferred_deal_currency(preferences, None) == preferences
    assert set_preferred_deal_currency(preferences, "") == preferences
    assert set_preferred_deal_currency(preferences, "USDT") == preferences
    assert set_preferred_deal_currency(preferences, "12$") == preferences


def test_normalize_user_preferences_preserves_user_task_list_columns() -> None:
    """An explicit task column selection should not be replaced by defaults."""
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


def test_normalize_user_preferences_preserves_valid_crm_list_sorting() -> None:
    """Valid saved sorting should survive login preference normalization."""
    preferences = {
        "crm": {
            "contacts": {
                "list": {
                    "columns": ["full_name"],
                    "sort_by": "last_activity_at",
                    "sort_order": "desc",
                }
            }
        }
    }

    normalized = normalize_user_preferences(preferences)

    assert normalized["crm"]["contacts"]["list"] == {
        "columns": ["full_name"],
        "sort_by": "last_activity_at",
        "sort_order": "desc",
    }


def test_normalize_user_preferences_removes_invalid_crm_list_sorting() -> None:
    """Malformed stored sorting should fall back to the frontend's existing defaults."""
    preferences = {
        "crm": {
            "tasks": {
                "list": {
                    "columns": ["subject"],
                    "sort_by": "unsupported",
                    "sort_order": "sideways",
                }
            }
        }
    }

    normalized = normalize_user_preferences(preferences)

    assert normalized["crm"]["tasks"]["list"] == {"columns": ["subject"]}


def test_merge_crm_list_sorting_preserves_columns_and_sibling_preferences() -> None:
    """Saving Accounts sorting must not replace columns or another collection's state."""
    preferences = {
        "notifications": {"email": True},
        "crm": {
            "accounts": {"list": {"columns": ["name", "owner.name"]}},
            "deals": {"preferred_currency": "INR"},
        },
    }

    updated = merge_crm_list_sorting(preferences, "accounts", "created_at", "desc")

    assert updated["crm"]["accounts"]["list"] == {
        "columns": ["name", "owner.name"],
        "sort_by": "created_at",
        "sort_order": "desc",
    }
    assert updated["crm"]["deals"]["preferred_currency"] == "INR"
    assert updated["notifications"] == {"email": True}


def test_calendar_sender_preference_normalizes_and_preserves_other_settings() -> None:
    """Calendar defaults should share User.preferences without replacing other branches."""
    preferences = {"notifications": {"email": True}}

    updated = merge_preferred_calendar_sender(preferences, " Sales@Contoso.COM ")

    assert preferred_calendar_sender(updated) == "sales@contoso.com"
    assert updated["notifications"] == {"email": True}


def test_calendar_sender_preference_can_be_cleared() -> None:
    """Clearing the default should leave deterministic inference available."""
    preferences = merge_preferred_calendar_sender({}, "sales@contoso.com")

    updated = merge_preferred_calendar_sender(preferences, None)

    assert preferred_calendar_sender(updated) is None
