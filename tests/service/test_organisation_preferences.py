from src.service.orgnisation.organisation_preferences import (
    has_crm_write_scope,
    normalize_organisation_preferences,
)


def test_missing_preferences_default_to_internal_crm_and_all_channels():
    preferences = normalize_organisation_preferences(None)

    assert preferences["crm"] == {
        "mode": "internal",
        "provider": "copilotgtm",
        "write_policy": {
            "allowed_scopes": preferences["crm"]["write_policy"]["allowed_scopes"],
        },
    }
    assert "deals:create" in preferences["crm"]["write_policy"]["allowed_scopes"]
    assert "leads:create" in preferences["crm"]["write_policy"]["allowed_scopes"]
    assert preferences["channels"] == {
        "enabled": ["slack", "whatsapp", "teams", "google_chat"],
    }
    assert preferences["capabilities"]["can_create_deals"] is True
    assert preferences["capabilities"]["can_create_leads"] is True


def test_empty_integrations_default_to_all_channels():
    preferences = normalize_organisation_preferences({"integrations": {}})

    assert preferences["channels"] == {
        "enabled": ["slack", "whatsapp", "teams", "google_chat"],
    }


def test_explicit_channels_are_preserved_and_cleaned():
    preferences = normalize_organisation_preferences(
        {"channels": {"enabled": ["slack", "slack", "unknown", "teams"]}}
    )

    assert preferences["channels"] == {"enabled": ["slack", "teams"]}


def test_legacy_copilot_gtm_source_is_normalized_to_copilotgtm_provider():
    preferences = normalize_organisation_preferences(
        {
            "crm": {"source": "copilot_gtm"},
            "integrations": {"enabled_sources": ["slack", "teams"]},
        }
    )

    assert preferences["crm"] == {
        "mode": "internal",
        "provider": "copilotgtm",
        "write_policy": {
            "allowed_scopes": preferences["crm"]["write_policy"]["allowed_scopes"],
        },
    }
    assert preferences["channels"] == {"enabled": ["slack", "teams"]}


def test_hubspot_is_external_and_disables_internal_write_capabilities():
    preferences = normalize_organisation_preferences({"crm": {"provider": "hubspot"}})

    assert preferences["crm"] == {
        "mode": "external",
        "provider": "hubspot",
        "write_policy": {
            "allowed_scopes": [],
        },
    }
    assert preferences["capabilities"] == {
        "can_create_deals": False,
        "can_edit_deals": False,
        "can_create_leads": False,
        "can_edit_leads": False,
        "can_manage_lead_forms": False,
    }


def test_write_scope_is_allowed_only_when_scope_is_in_policy():
    assert has_crm_write_scope({"crm": {"provider": "copilotgtm"}}, "deals:create") is True
    assert has_crm_write_scope({"crm": {"provider": "hubspot"}}, "deals:create") is False
    assert (
        has_crm_write_scope(
            {
                "crm": {
                    "provider": "copilotgtm",
                    "write_policy": {"allowed_scopes": ["leads:create"]},
                }
            },
            "deals:create",
        )
        is False
    )
