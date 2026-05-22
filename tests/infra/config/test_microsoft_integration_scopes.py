from src.infra.config.microsoft_integration_scopes import (
    get_microsoft_admin_consent_scope_version,
    get_microsoft_admin_consent_scopes,
)


def test_microsoft_admin_consent_scopes_are_union_for_all_user_owned_integrations() -> None:
    assert get_microsoft_admin_consent_scopes() == (
        "offline_access",
        "User.Read",
        "Mail.Read",
        "Mail.Send",
        "Calendars.ReadWrite",
        "OnlineMeetings.Read",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "Chat.Read",
    )


def test_microsoft_admin_consent_scope_version_is_stable_for_current_union() -> None:
    assert get_microsoft_admin_consent_scope_version() == "3eff55a40e48"
