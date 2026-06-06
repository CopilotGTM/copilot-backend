from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from src.api.routers.crm.tasks_handler import (
    _build_task_assignee,
    _build_task_due_metadata,
)


def test_task_assignee_uses_email_filter_key_across_owner_id_sources() -> None:
    crm_owner_id = uuid4()
    public_user_id = uuid4()

    synced = SimpleNamespace(
        owner_id=crm_owner_id,
        owner_name="Nikhil Singhal",
        owner_email="Nikhil@Example.com",
        external_assignee_email=None,
        external_assignee_name=None,
        properties={},
    )
    manual = SimpleNamespace(
        owner_id=None,
        owner_name="Nikhil Singhal",
        owner_email="nikhil@example.com",
        external_assignee_email=None,
        external_assignee_name=None,
        properties={"assigned_to_public_user_id": str(public_user_id)},
    )

    assert _build_task_assignee(synced).filter_key == "email:nikhil@example.com"
    assert _build_task_assignee(manual).filter_key == "email:nikhil@example.com"
    assert _build_task_assignee(synced).crm_user_id == crm_owner_id
    assert _build_task_assignee(manual).public_user_id == public_user_id


def test_task_assignee_prefers_external_assignee_when_present() -> None:
    owner_id = uuid4()
    task = SimpleNamespace(
        owner_id=owner_id,
        owner_name="Owner",
        owner_email="owner@example.com",
        external_assignee_email="External@Example.com",
        external_assignee_name="External Person",
        properties={},
    )

    assigned_to = _build_task_assignee(task)

    assert assigned_to.kind == "external"
    assert assigned_to.filter_key == "email:external@example.com"
    assert assigned_to.name == "External Person"
    assert assigned_to.email == "external@example.com"


def test_all_day_due_metadata_preserves_local_calendar_date() -> None:
    task = SimpleNamespace(
        due_date=datetime(2026, 5, 19, 18, 30, tzinfo=UTC),
        is_all_day=True,
        properties={"due_timezone": "Asia/Kolkata", "due_local_date": "2026-05-20"},
    )

    metadata = _build_task_due_metadata(task)

    assert metadata["due_timezone"] == "Asia/Kolkata"
    assert metadata["due_local_date"] == "2026-05-20"
    assert metadata["is_all_day"] is True


def test_all_day_due_metadata_falls_back_to_timezone_conversion() -> None:
    task = SimpleNamespace(
        due_date=datetime(2026, 5, 19, 18, 30, tzinfo=UTC),
        is_all_day=True,
        properties={"due_timezone": "Asia/Kolkata"},
    )

    metadata = _build_task_due_metadata(task)

    assert metadata["due_local_date"] == "2026-05-20"
