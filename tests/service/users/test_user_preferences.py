from src.service.users.user_preferences import normalize_user_preferences


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
