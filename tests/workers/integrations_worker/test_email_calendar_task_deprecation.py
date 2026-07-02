from pathlib import Path

import src.workers.integrations_worker.tasks as integration_tasks
from src.workers.integrations_worker.config import TASK_LIMITS, IntegrationsWorkerConfig
from src.workers.integrations_worker.tasks import calendar, email

OLD_EMAIL_CALENDAR_TASK_NAMES = {
    "integrations.sync_calendar_all_orgs",
    "integrations.sync_calendar_org",
    "integrations.sync_email_all_orgs",
    "integrations.sync_email_org",
}


def test_email_calendar_packages_export_only_event_based_integration_tasks() -> None:
    """Email/calendar sync now runs from event-based per-integration dispatch only."""
    assert calendar.__all__ == ["sync_calendar_integration_task"]
    assert email.__all__ == ["sync_email_integration_task"]
    assert "sync_calendar_integration_task" in integration_tasks.__all__
    assert "sync_email_integration_task" in integration_tasks.__all__
    assert "sync_calendar_all_orgs_task" not in integration_tasks.__all__
    assert "sync_calendar_org_task" not in integration_tasks.__all__
    assert "sync_email_all_orgs_task" not in integration_tasks.__all__
    assert "sync_email_org_task" not in integration_tasks.__all__


def test_email_calendar_org_and_all_org_tasks_are_deprecated_files_only() -> None:
    """The old scheduled fanout files should not live in the active task packages."""
    tasks_dir = Path("src/workers/integrations_worker/tasks")

    assert not (tasks_dir / "calendar" / "all_orgs_task.py").exists()
    assert not (tasks_dir / "calendar" / "org_task.py").exists()
    assert not (tasks_dir / "email" / "all_orgs_task.py").exists()
    assert not (tasks_dir / "email" / "org_task.py").exists()

    assert (tasks_dir / "deprecated" / "calendar" / "all_orgs_task.py").exists()
    assert (tasks_dir / "deprecated" / "calendar" / "org_task.py").exists()
    assert (tasks_dir / "deprecated" / "email" / "all_orgs_task.py").exists()
    assert (tasks_dir / "deprecated" / "email" / "org_task.py").exists()


def test_email_calendar_org_and_all_org_routes_are_removed() -> None:
    """Removed task names should not be schedulable from active worker config."""
    routes = IntegrationsWorkerConfig().task_routes

    assert OLD_EMAIL_CALENDAR_TASK_NAMES.isdisjoint(TASK_LIMITS)
    assert OLD_EMAIL_CALENDAR_TASK_NAMES.isdisjoint(routes)
    assert "integrations.sync_calendar_integration" in TASK_LIMITS
    assert "integrations.sync_email_integration" in TASK_LIMITS
    assert "integrations.sync_calendar_integration" in routes
    assert "integrations.sync_email_integration" in routes
