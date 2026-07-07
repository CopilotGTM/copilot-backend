from src.service.orgnisation.organisation_preferences import (
    BOT_JOIN_POLICY_EXTERNAL_ONLY,
    has_crm_write_scope,
    has_google_gmail_read_scope_enabled,
    merge_organisation_app_settings,
    normalize_organisation_preferences,
    organisation_preferences_for_settings,
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
    assert "meetings:create" in preferences["crm"]["write_policy"]["allowed_scopes"]
    assert "meetings:update" in preferences["crm"]["write_policy"]["allowed_scopes"]
    assert preferences["channels"] == {
        "enabled": ["slack", "whatsapp", "teams", "google_chat"],
    }
    assert preferences["meeting_recording"] == {
        "bot_join_policy": BOT_JOIN_POLICY_EXTERNAL_ONLY,
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


def test_missing_google_gmail_preference_disables_read_scope():
    assert has_google_gmail_read_scope_enabled(None) is False
    assert has_google_gmail_read_scope_enabled({"integrations": {"google": {}}}) is False
    assert (
        has_google_gmail_read_scope_enabled(
            {"integrations": {"google": {"gmail_read_enabled": False}}}
        )
        is False
    )


def test_explicit_google_gmail_preference_enables_read_scope():
    assert (
        has_google_gmail_read_scope_enabled(
            {"integrations": {"google": {"gmail_read_enabled": True}}}
        )
        is True
    )


def test_merge_organisation_app_settings_preserves_google_scope_preferences():
    preferences = merge_organisation_app_settings(
        {"integrations": {"microsoft": {"admin_consent": {"granted": True}}}},
        google_gmail_read_enabled=True,
    )

    assert preferences["integrations"]["google"] == {"gmail_read_enabled": True}
    assert preferences["integrations"]["microsoft"] == {"admin_consent": {"granted": True}}


def test_meeting_recording_policy_accepts_allowed_values():
    for policy in ("all", "external_only", "never"):
        preferences = normalize_organisation_preferences(
            {"meeting_recording": {"bot_join_policy": policy}}
        )

        assert preferences["meeting_recording"]["bot_join_policy"] == policy


def test_settings_preferences_include_meeting_recording_policy_options():
    preferences = organisation_preferences_for_settings(None)

    assert preferences["meeting_recording"] == {
        "bot_join_policy": BOT_JOIN_POLICY_EXTERNAL_ONLY,
        "bot_join_policy_options": [
            {"value": "external_only", "label": "External meetings only"},
            {"value": "all", "label": "All meetings"},
            {"value": "never", "label": "Never join"},
        ],
    }


def test_invalid_meeting_recording_policy_defaults_to_external_only():
    preferences = normalize_organisation_preferences(
        {"meeting_recording": {"bot_join_policy": "record_everything"}}
    )

    assert preferences["meeting_recording"]["bot_join_policy"] == BOT_JOIN_POLICY_EXTERNAL_ONLY


def test_merge_organisation_app_settings_updates_meeting_recording_policy():
    preferences = merge_organisation_app_settings(
        {"meeting_recording": {"bot_join_policy": "external_only"}},
        meeting_recording_bot_join_policy="never",
    )

    assert preferences["meeting_recording"] == {"bot_join_policy": "never"}


def test_meetings_write_scopes_are_allowed_for_internal_crm() -> None:
    assert has_crm_write_scope({"crm": {"provider": "copilotgtm"}}, "meetings:create") is True
    assert has_crm_write_scope({"crm": {"provider": "copilotgtm"}}, "meetings:update") is True
