"""Guard every configured integration/deal route during threaded execution."""

import inspect

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from src.infra.utils.async_runner import WorkerAsyncTimeoutError
from src.workers.deal_worker.celery_app import app as deal_app
from src.workers.integrations_worker.celery_app import app as integrations_app


@pytest.mark.parametrize("app", [integrations_app, deal_app])
def test_registered_routes_have_a_soft_deadline(app) -> None:
    app.loader.import_default_modules()

    registered_routes = set(app.conf.task_routes) & set(app.tasks)
    assert registered_routes
    for task_name in registered_routes:
        task = app.tasks[task_name]
        assert task.soft_time_limit or app.conf.task_soft_time_limit


def test_routed_tasks_enter_shared_async_runtime_or_are_sync_orchestrators() -> None:
    integrations_app.loader.import_default_modules()
    deal_app.loader.import_default_modules()
    sync_orchestrators = {
        # These services call run_async at their I/O boundary while keeping
        # short synchronous database orchestration in the Celery task thread.
        "deal.cleanup_stale_jobs_org",
        "deal_asset.post_upload",
    }

    for app in (integrations_app, deal_app):
        registered_routes = set(app.conf.task_routes) & set(app.tasks)
        for task_name in registered_routes:
            if task_name in sync_orchestrators:
                continue
            assert "run_async(" in inspect.getsource(app.tasks[task_name].run), task_name


def test_cooperative_deadline_uses_existing_soft_limit_handlers() -> None:
    assert issubclass(WorkerAsyncTimeoutError, SoftTimeLimitExceeded)
