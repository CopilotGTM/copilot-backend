import logging

from src.infra.observability.context import reset_context
from src.workers.base import celery_base


class _DummyTaskRequest:
    headers: dict[str, str] = {}
    delivery_info: dict[str, str] = {"routing_key": "deal"}


class _DummyTask:
    name = "deal.match_calendar_to_deals_org"

    def __init__(self) -> None:
        self.request = _DummyTaskRequest()


def test_celery_task_memory_observability_is_disabled_by_default(monkeypatch, mocker) -> None:
    monkeypatch.delenv("CELERY_TASK_MEMORY_LOGGING_ENABLED", raising=False)
    rss_mock = mocker.patch("src.workers.base.celery_base.get_current_rss_mb", return_value=100.0)
    metric_mock = mocker.patch("src.workers.base.celery_base.record_celery_task_memory")

    celery_base.create_celery_app("unit_worker", celery_base_config(), "tests.fake_tasks")
    task = _DummyTask()

    try:
        celery_base.task_prerun.send(sender=None, task_id="task-123", task=task)
        celery_base.task_postrun.send(sender=None, task_id="task-123", task=task, state="SUCCESS")
    finally:
        reset_context()

    rss_mock.assert_not_called()
    metric_mock.assert_not_called()


def test_celery_task_memory_observability_logs_and_records_when_enabled(
    caplog, monkeypatch, mocker
) -> None:
    monkeypatch.setenv("CELERY_TASK_MEMORY_LOGGING_ENABLED", "true")
    monkeypatch.setenv("CELERY_TASK_MEMORY_LOGGING_MIN_DELTA_MB", "1")
    mocker.patch(
        "src.workers.base.celery_base.get_current_rss_mb",
        side_effect=[100.0, 104.5],
    )
    metric_mock = mocker.patch("src.workers.base.celery_base.record_celery_task_memory")

    celery_base.create_celery_app("unit_worker", celery_base_config(), "tests.fake_tasks")
    task = _DummyTask()

    with caplog.at_level(logging.INFO, logger="celery_task"):
        try:
            celery_base.task_prerun.send(sender=None, task_id="task-123", task=task)
            celery_base.task_postrun.send(
                sender=None,
                task_id="task-123",
                task=task,
                state="SUCCESS",
            )
        finally:
            reset_context()

    metric_mock.assert_called_once_with(
        task_name="deal.match_calendar_to_deals_org",
        queue="deal",
        status="SUCCESS",
        rss_mb=104.5,
        rss_delta_mb=4.5,
    )
    record = next(
        item for item in caplog.records if getattr(item, "event", None) == "celery_task_memory"
    )
    assert record.task_name == "deal.match_calendar_to_deals_org"
    assert record.queue == "deal"
    assert record.status == "SUCCESS"
    assert record.rss_start_mb == 100.0
    assert record.rss_end_mb == 104.5
    assert record.rss_delta_mb == 4.5
    assert record.process_id > 0


def test_celery_task_memory_observability_honors_delta_threshold(
    caplog, monkeypatch, mocker
) -> None:
    monkeypatch.setenv("CELERY_TASK_MEMORY_LOGGING_ENABLED", "true")
    monkeypatch.setenv("CELERY_TASK_MEMORY_LOGGING_MIN_DELTA_MB", "10")
    mocker.patch(
        "src.workers.base.celery_base.get_current_rss_mb",
        side_effect=[100.0, 104.5],
    )
    metric_mock = mocker.patch("src.workers.base.celery_base.record_celery_task_memory")

    celery_base.create_celery_app("unit_worker", celery_base_config(), "tests.fake_tasks")
    task = _DummyTask()

    with caplog.at_level(logging.INFO, logger="celery_task"):
        try:
            celery_base.task_prerun.send(sender=None, task_id="task-123", task=task)
            celery_base.task_postrun.send(
                sender=None,
                task_id="task-123",
                task=task,
                state="SUCCESS",
            )
        finally:
            reset_context()

    metric_mock.assert_not_called()
    assert not any(getattr(item, "event", None) == "celery_task_memory" for item in caplog.records)


def celery_base_config():
    class _Config:
        broker_url = "redis://localhost:6379/0"
        backend_url = "redis://localhost:6379/0"
        ssl_config = None

    return _Config()
