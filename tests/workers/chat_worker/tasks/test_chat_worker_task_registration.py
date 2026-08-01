"""Task-registration contracts that keep chat-worker imports explicit and lazy."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys

EXPECTED_TASK_MODULES = [
    "src.workers.chat_worker.tasks.agent_run_tasks",
    "src.workers.chat_worker.tasks.automation_event_tasks",
    "src.workers.chat_worker.tasks.bot_message_processing",
    "src.workers.chat_worker.tasks.message_processing",
    "src.workers.chat_worker.tasks.post_upload",
    "src.workers.chat_worker.tasks.reminder_tasks",
    "src.workers.chat_worker.tasks.stale_job_cleanup_tasks",
    "src.workers.chat_worker.tasks.test_tasks",
    "src.workers.chat_worker.tasks.voice_realtime",
]

EXPECTED_TASK_NAMES = {
    "attachments.post_upload",
    "chat.activate_automation_event",
    "chat.cleanup_stale_jobs_all_orgs",
    "chat.cleanup_stale_jobs_org",
    "chat.execute_reminder",
    "chat.execute_agent_run",
    "chat.ping",
    "chat.poll_due_reminders",
    "chat.process_bot_message",
    "chat.process_message",
    "chat.recovery_sweep_reminders",
    "chat.recover_agent_runs",
    "chat.test_task",
    "chat.voice_realtime_sideband",
}


def test_chat_tasks_package_import_does_not_import_task_modules():
    """Importing the package must not eagerly load task modules or infrastructure."""
    code = (
        "import importlib, json, sys; "
        "expected = "
        f"{EXPECTED_TASK_MODULES!r}; "
        "importlib.import_module('src.workers.chat_worker.tasks'); "
        "print(json.dumps([name for name in expected if name in sys.modules]))"
    )
    completed = subprocess.run(  # noqa: S603 - static Python command used for import isolation.
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    imported_task_modules = json.loads(completed.stdout.strip().splitlines()[-1])
    assert imported_task_modules == []


def test_chat_worker_includes_explicit_task_modules_and_registers_task_names():
    """Every configured module must load and register the expected task names."""
    celery_app_module = importlib.import_module("src.workers.chat_worker.celery_app")

    assert list(celery_app_module.app.conf.include) == EXPECTED_TASK_MODULES

    celery_app_module.app.loader.import_default_modules()

    missing_tasks = EXPECTED_TASK_NAMES - set(celery_app_module.app.tasks)
    assert missing_tasks == set()


def test_high_frequency_cleanup_tasks_do_not_store_redis_results():
    """The 15-second recovery fanout must not create one backend key per task."""
    celery_app_module = importlib.import_module("src.workers.chat_worker.celery_app")
    celery_app_module.app.loader.import_default_modules()

    assert celery_app_module.app.tasks["chat.cleanup_stale_jobs_all_orgs"].ignore_result is True
    assert celery_app_module.app.tasks["chat.cleanup_stale_jobs_org"].ignore_result is True
