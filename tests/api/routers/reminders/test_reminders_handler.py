from src.api.routers.reminders.reminders_handler import (
    _delivery_context_is_ready,
    _normalize_delivery_context,
    _provider_name,
)


def test_normalize_delivery_context_adds_slack_team_id_from_external_tenant() -> None:
    normalized = _normalize_delivery_context(
        delivery_channel="slack",
        conversation_context={"channel_id": "C123"},
        external_tenant_id="T123",
    )

    assert normalized == {"channel_id": "C123", "team_id": "T123"}


def test_delivery_context_is_ready_requires_complete_teams_context() -> None:
    assert _delivery_context_is_ready(
        "teams",
        {"service_url": "https://service", "conversation_id": "conv-123"},
    )
    assert not _delivery_context_is_ready(
        "teams",
        {"service_url": "https://service", "conversation_id": ""},
    )


def test_delivery_context_is_ready_requires_complete_slack_context() -> None:
    assert _delivery_context_is_ready(
        "slack",
        {"team_id": "T123", "channel_id": "C123"},
    )
    assert not _delivery_context_is_ready(
        "slack",
        {"channel_id": "C123"},
    )


def test_delivery_context_is_ready_requires_complete_whatsapp_context() -> None:
    assert _delivery_context_is_ready(
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )
    assert not _delivery_context_is_ready(
        "whatsapp",
        {"phone_number_id": "phone-123"},
    )


def test_provider_name_accepts_enum_and_string() -> None:
    assert _provider_name("slack") == "slack"
