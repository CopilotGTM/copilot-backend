from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.service.crm.tasks.read_service import _merge_assignee_rows
from src.service.crm.tasks.write_service import TaskWriteService


def test_tasks_vertical_module_exists() -> None:
    assert Path("src/service/crm/tasks/__init__.py").exists()
    assert Path("src/service/crm/tasks/contracts.py").exists()
    assert Path("src/service/crm/tasks/read_service.py").exists()
    assert Path("src/service/crm/tasks/write_service.py").exists()
    assert Path("src/service/crm/tasks/reminder_service.py").exists()
    assert Path("src/service/crm/tasks/notification_service.py").exists()
    assert Path("src/api/presenters/crm/task_presenter.py").exists()


def test_task_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "async def create_task" not in source
    assert "async def update_task" not in source
    assert "async def delete_task" not in source
    assert "CRMActivity(" not in source


def test_task_read_service_uses_crm_tasks_as_source_table() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "CRMTask" in source
    assert "select(CRMTask)" in source
    assert "select(CRMActivity)" not in source
    assert "cast(Any, CRMActivity)" not in source


def test_task_read_service_owns_org_scoped_reminder_enrichment() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "UserReminder" in source
    assert "TaskReminderSummary" in source
    assert "col(UserReminder.organisation_id) == self.organisation_id" in source
    assert "col(UserReminder.activity_id).in_(list(task_ids))" in source


def test_task_write_service_uses_crm_tasks_as_source_table() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "CRMTask" in source
    assert "CRMTask(" in source
    assert "CRMActivity(" not in source


def test_task_projection_boundary_is_explicit() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "ActivityFeedProjectionService" in source
    assert Path("src/service/crm/activity_feed_projection_service.py").exists()


def test_provider_task_writers_target_crm_tasks_not_activity_rows() -> None:
    writer_paths = [
        Path("src/service/integrations/crm/records/hubspot/tasks/task_writer.py"),
        Path("src/service/integrations/crm/records/salesforce/tasks/task_writer.py"),
        Path("src/service/crm/internal/deal_task_automation_service.py"),
    ]

    for path in writer_paths:
        source = path.read_text()
        assert "CRMTask" in source
        assert "CRMActivity(" not in source
        assert "select(CRMActivity)" not in source
        assert "pg_insert(CRMActivity)" not in source
        assert "local_object_type=CRMSourceObjectType.ACTIVITY" not in source


def test_salesforce_cdc_task_deletes_target_crm_tasks_not_activity_rows() -> None:
    source = Path(
        "src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py"
    ).read_text()
    task_delete_source = source.split("async def _soft_delete_task", 1)[1].split(
        "async def _deactivate_user", 1
    )[0]

    assert "CRMTask" in task_delete_source
    assert "select(CRMActivity)" not in task_delete_source
    assert "CRMActivityType.TASK" not in task_delete_source


def test_task_write_service_does_not_import_router_or_agent_layers() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "src.api." not in source
    assert "src.agents." not in source


def test_task_handler_does_not_own_task_presentation_or_reminder_sql() -> None:
    source = Path("src/api/routers/crm/tasks_handler.py").read_text()

    blocked_tokens = [
        "UserReminder",
        "ReminderStatus",
        "select(",
        "func",
        "col(",
        "_enrich_task",
        "_has_active_reminder",
        "_get_reminder_dict",
    ]
    for token in blocked_tokens:
        assert token not in source


def test_legacy_task_services_are_facades_after_migration() -> None:
    access_source = Path("src/service/crm/access/tasks_access_service.py").read_text()
    write_source = Path("src/service/crm/internal/tasks_write_service.py").read_text()

    assert "TaskReadService" in access_source
    assert "TaskWriteService" in write_source


def test_task_due_payload_enforces_due_shape_at_service_level() -> None:
    service = TaskWriteService(
        cast(Any, object()),
        uuid4(),
        uuid4(),
        user_timezone="Asia/Kolkata",
    )

    timed_due = datetime(2026, 3, 24, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    due_at, due_day, is_all_day, metadata = service._normalize_due_payload({"due_date": timed_due})

    assert due_at == datetime(2026, 3, 24, 5, 0, tzinfo=UTC)
    assert due_day == date(2026, 3, 24)
    assert is_all_day is False
    assert "due_timezone" not in metadata
    assert "due_local_date" not in metadata

    all_day_due = datetime(2026, 3, 24, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    due_at, due_day, is_all_day, metadata = service._normalize_due_payload(
        {"due_date": all_day_due, "is_all_day": True}
    )

    assert due_at is None
    assert due_day == date(2026, 3, 24)
    assert is_all_day is True
    assert metadata["due_timezone"] == "Asia/Kolkata"
    assert metadata["due_local_date"] == "2026-03-24"


def test_task_assignee_options_merge_by_emitted_filter_key() -> None:
    assignee_id_a = uuid4()
    assignee_id_b = uuid4()

    rows = [
        SimpleNamespace(
            assigned_to_user_id=assignee_id_a,
            assigned_to_name="Alex A",
            assigned_to_email="Alex@Example.com",
            assignee_kind=None,
            task_count=2,
        ),
        SimpleNamespace(
            assigned_to_user_id=assignee_id_b,
            assigned_to_name="Alex B",
            assigned_to_email="alex@example.com",
            assignee_kind=None,
            task_count=3,
        ),
        SimpleNamespace(
            assigned_to_user_id=None,
            assigned_to_name="External Person",
            assigned_to_email=None,
            assignee_kind="external",
            task_count=4,
        ),
    ]

    merged = _merge_assignee_rows(rows, limit=10)

    assert len(merged) == 2
    email_option = next(
        row for row in merged if row["task"].assigned_to_email.lower() == "alex@example.com"
    )
    external_option = next(
        row for row in merged if row["task"].assigned_to_name == "External Person"
    )
    assert email_option["task_count"] == 5
    assert external_option["task_count"] == 4


def test_task_write_service_clears_stale_owner_for_name_only_external_assignment() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert 'if "external_assignee_name" in payload:' in source
    assert 'if "external_assignee_email" not in payload:' in source
    assert "task.assigned_to_user_id = None" in source
    assert "task.assigned_to_email = None" in source
    assert 'metadata.pop("assigned_to_public_user_id", None)' in source
