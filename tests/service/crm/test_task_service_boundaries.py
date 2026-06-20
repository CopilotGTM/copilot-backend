from __future__ import annotations

from pathlib import Path


def test_tasks_vertical_module_exists() -> None:
    assert Path("src/service/crm/tasks/__init__.py").exists()
    assert Path("src/service/crm/tasks/contracts.py").exists()
    assert Path("src/service/crm/tasks/read_service.py").exists()
    assert Path("src/service/crm/tasks/write_service.py").exists()
    assert Path("src/service/crm/tasks/reminder_service.py").exists()
    assert Path("src/service/crm/tasks/notification_service.py").exists()


def test_task_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "async def create_task" not in source
    assert "async def update_task" not in source
    assert "async def delete_task" not in source
    assert "CRMActivity(" not in source


def test_task_write_service_does_not_import_router_or_agent_layers() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "src.api." not in source
    assert "src.agents." not in source


def test_legacy_task_services_are_facades_after_migration() -> None:
    access_source = Path("src/service/crm/access/tasks_access_service.py").read_text()
    write_source = Path("src/service/crm/internal/tasks_write_service.py").read_text()

    assert "TaskReadService" in access_source
    assert "TaskWriteService" in write_source
