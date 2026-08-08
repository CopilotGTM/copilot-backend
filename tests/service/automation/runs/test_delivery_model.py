"""Persistence invariants for agent output notifications."""

from src.domain.models.organisation_tables import agent_models
from src.domain.models.organisation_tables.sync import notification_delivery


def test_agent_output_reuses_the_generic_notification_delivery_ledger() -> None:
    assert not hasattr(agent_models, "AgentRunDelivery")
    delivery_model = notification_delivery.NotificationDelivery
    delivery_channel = notification_delivery.NotificationDeliveryChannel
    delivery_status = notification_delivery.NotificationDeliveryStatus
    run_status = agent_models.AgentRunStatus
    table = delivery_model.__table__
    constraint_names = {item.name for item in table.constraints}
    index_names = {item.name for item in table.indexes}

    assert delivery_channel.EMAIL.value == "email"
    assert delivery_status.NEEDS_ATTENTION.value == "NEEDS_ATTENTION"
    assert "delivery_failed" not in {status.value for status in run_status}
    assert "uq_notification_delivery_target" in constraint_names
    assert "ix_notification_delivery_recovery" in index_names
    assert "ix_notification_delivery_stale_claim" in index_names
    for field in (
        "next_attempt_at",
        "claimed_at",
        "claimed_by",
        "claim_token",
        "provider_resource_id",
        "error_code",
    ):
        assert field in table.c
