from src.service.users.user_preferences import (
    merge_navigation_preferences,
    normalize_navigation_preferences,
    normalize_user_preferences,
)


def test_navigation_defaults_open_broad_sections_and_close_crm_fields() -> None:
    navigation = normalize_navigation_preferences({})

    assert all(
        open_state
        for key, open_state in navigation["expanded_sections"].items()
        if key != "settings_crm_fields"
    )
    assert navigation["expanded_sections"]["settings_crm_fields"] is False
    assert navigation["last_settings_item"] == "integrations"
    assert navigation["crm_fields_entity"] == "deal"


def test_navigation_normalization_ignores_unknown_and_invalid_values() -> None:
    navigation = normalize_navigation_preferences(
        {
            "expanded_sections": {"workspace": False, "records": "false", "unknown": True},
            "last_settings_item": "/not-a-settings-route",
            "crm_fields_entity": "contact",
        }
    )

    assert navigation["expanded_sections"]["workspace"] is False
    assert navigation["expanded_sections"]["records"] is True
    assert "unknown" not in navigation["expanded_sections"]
    assert navigation["last_settings_item"] == "integrations"
    assert navigation["crm_fields_entity"] == "deal"

    malformed = normalize_navigation_preferences(
        {"last_settings_item": [], "crm_fields_entity": {}}
    )
    assert malformed["last_settings_item"] == "integrations"
    assert malformed["crm_fields_entity"] == "deal"


def test_navigation_merge_preserves_unrelated_user_preferences() -> None:
    updated = merge_navigation_preferences(
        {"crm": {"deals": {"preferred_currency": "INR"}}, "notifications": {"email": True}},
        {
            "expanded_sections": {"analytics": False},
            "last_settings_item": "pipelines",
            "crm_fields_entity": "lead",
        },
    )

    assert updated["crm"]["deals"]["preferred_currency"] == "INR"
    assert updated["notifications"]["email"] is True
    assert updated["navigation"]["expanded_sections"]["analytics"] is False
    assert updated["navigation"]["last_settings_item"] == "pipelines"
    assert normalize_user_preferences(updated)["navigation"] == updated["navigation"]
