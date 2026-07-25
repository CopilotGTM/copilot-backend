"""Contract tests for participant-owned cross-layer values."""

from src.service.crm.participants.contracts import StakeholderChangeType


def test_stakeholder_change_type_names_and_values_are_stable() -> None:
    """Celery producers and consumers should continue exchanging the same strings."""
    assert [(member.name, member.value) for member in StakeholderChangeType] == [
        ("NEW_DEAL", "NEW_DEAL"),
        ("PARTICIPANT_ADDED", "PARTICIPANT_ADDED"),
        ("PARTICIPANT_UPDATED", "PARTICIPANT_UPDATED"),
        ("PARTICIPANT_REMOVED", "PARTICIPANT_REMOVED"),
        ("MANUAL_REFRESH", "MANUAL_REFRESH"),
    ]
    assert all(isinstance(member, str) for member in StakeholderChangeType)
