from __future__ import annotations

import importlib
import json
import subprocess
import sys

EXPECTED_TASK_MODULES = [
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
    "chat.cleanup_stale_jobs_all_orgs",
    "chat.cleanup_stale_jobs_org",
    "chat.execute_reminder",
    "chat.ping",
    "chat.poll_due_reminders",
    "chat.process_bot_message",
    "chat.process_message",
    "chat.recovery_sweep_reminders",
    "chat.test_task",
    "chat.voice_realtime_sideband",
}


def test_chat_tasks_package_import_does_not_import_task_modules():
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
    celery_app_module = importlib.import_module("src.workers.chat_worker.celery_app")

    assert list(celery_app_module.app.conf.include) == EXPECTED_TASK_MODULES

    celery_app_module.app.loader.import_default_modules()

    missing_tasks = EXPECTED_TASK_NAMES - set(celery_app_module.app.tasks)
    assert missing_tasks == set()
