from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from src.api.presenters.crm.task_presenter import (
    build_task_due_metadata,
    present_task_assignee,
)


def test_task_assignee_uses_email_filter_key_across_owner_id_sources() -> None:
    crm_owner_id = uuid4()
    public_user_id = uuid4()

    synced = SimpleNamespace(
        assigned_to_user_id=crm_owner_id,
        assigned_to_name="Nikhil Singhal",
        assigned_to_email="Nikhil@Example.com",
        task_metadata={},
    )
    manual = SimpleNamespace(
        assigned_to_user_id=None,
        assigned_to_name="Nikhil Singhal",
        assigned_to_email="nikhil@example.com",
        task_metadata={"assigned_to_public_user_id": str(public_user_id)},
    )

    assert present_task_assignee(synced).filter_key == "email:nikhil@example.com"
    assert present_task_assignee(manual).filter_key == "email:nikhil@example.com"
    assert present_task_assignee(synced).crm_user_id == crm_owner_id
    assert present_task_assignee(manual).public_user_id == public_user_id


def test_task_assignee_prefers_external_assignee_when_present() -> None:
    owner_id = uuid4()
    task = SimpleNamespace(
        assigned_to_user_id=owner_id,
        assigned_to_name="External Person",
        assigned_to_email="External@Example.com",
        task_metadata={"assignee_kind": "external"},
    )

    assigned_to = present_task_assignee(task)

    assert assigned_to.kind == "external"
    assert assigned_to.filter_key == "email:external@example.com"
    assert assigned_to.name == "External Person"
    assert assigned_to.email == "external@example.com"


def test_all_day_due_metadata_preserves_local_calendar_date() -> None:
    task = SimpleNamespace(
        due_at=None,
        due_date=date(2026, 5, 20),
        is_all_day=True,
        task_metadata={"due_timezone": "Asia/Kolkata", "due_local_date": "2026-05-20"},
    )

    metadata = build_task_due_metadata(task)

    assert metadata["due_timezone"] == "Asia/Kolkata"
    assert metadata["due_local_date"] == "2026-05-20"
    assert metadata["is_all_day"] is True


def test_all_day_due_metadata_falls_back_to_timezone_conversion() -> None:
    task = SimpleNamespace(
        due_at=datetime(2026, 5, 19, 18, 30, tzinfo=UTC),
        due_date=None,
        is_all_day=True,
        task_metadata={"due_timezone": "Asia/Kolkata"},
    )

    metadata = build_task_due_metadata(task)

    assert metadata["due_local_date"] == "2026-05-20"
