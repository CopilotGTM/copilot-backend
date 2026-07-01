"""Worker delivery guarantees that keep Kubernetes restarts from dropping tasks."""

from src.workers.base import celery_base


class _DummyWorkerConfig:
    broker_url = "memory://"
    backend_url = "cache+memory://"
    ssl_config = None


def test_create_celery_app_requeues_tasks_when_worker_child_is_lost() -> None:
    app = celery_base.create_celery_app(
        "unit_worker",
        _DummyWorkerConfig(),
        "tests.fake_tasks",
    )

    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
