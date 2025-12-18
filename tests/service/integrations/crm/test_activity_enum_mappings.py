from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivityStatus,
    CRMActivityType,
)
from src.service.integrations.crm.entities.activity_enum_mappings import (
    normalize_activity_priority,
    normalize_activity_status,
    normalize_activity_type,
)


def test_normalize_activity_status_known_values() -> None:
    assert normalize_activity_status("NOT_STARTED") == CRMActivityStatus.NOT_STARTED.value
    assert normalize_activity_status("In Progress") == CRMActivityStatus.IN_PROGRESS.value
    assert normalize_activity_status("Waiting on someone else") == CRMActivityStatus.WAITING.value


def test_normalize_activity_status_unknown_returns_none() -> None:
    assert normalize_activity_status("SOME_NEW_STATUS") is None


def test_normalize_activity_type_known_values() -> None:
    assert normalize_activity_type("CALL") == CRMActivityType.CALL.value
    assert normalize_activity_type("deadline") == CRMActivityType.TASK.value


def test_normalize_activity_type_unknown_returns_other() -> None:
    assert normalize_activity_type("FAX") == CRMActivityType.OTHER.value


def test_normalize_activity_priority_known_values() -> None:
    assert normalize_activity_priority("High") == CRMActivityPriority.HIGH.value
    assert normalize_activity_priority("HIGHEST") == CRMActivityPriority.URGENT.value


def test_normalize_activity_priority_unknown_returns_none_bucket() -> None:
    assert normalize_activity_priority("P0") == CRMActivityPriority.NONE.value
