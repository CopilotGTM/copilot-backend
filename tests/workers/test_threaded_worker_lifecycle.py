"""Lifecycle contracts for Celery workers that share one asyncio loop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "src.workers.integrations_worker.celery_app",
        "src.workers.deal_worker.celery_app",
    ],
)
def test_target_worker_starts_and_stops_shared_loop(module_name, monkeypatch):
    """Each threaded worker must own its async loop for its full process lifetime."""
    module = __import__(module_name, fromlist=["celery_app"])
    calls: list[str] = []
    monkeypatch.setattr(module, "start_shared_loop", lambda: calls.append("start"))
    monkeypatch.setattr(module, "initialize_redis_pool", lambda: calls.append("redis_start"))
    monkeypatch.setattr(module, "close_redis_pool", lambda: calls.append("redis_stop"))
    monkeypatch.setattr(module, "stop_shared_loop", lambda: calls.append("stop"))
    sender = SimpleNamespace(app=SimpleNamespace(main=module.app.main))

    module.worker_ready_handler(sender=sender)
    module.worker_shutdown_handler(sender=sender)

    assert calls == ["start", "redis_start", "redis_stop", "stop"]


@pytest.mark.parametrize(
    "module_name",
    [
        "src.workers.integrations_worker.celery_app",
        "src.workers.deal_worker.celery_app",
    ],
)
def test_foreign_worker_signal_does_not_touch_shared_loop(module_name, monkeypatch):
    """Importing multiple Celery apps must not let one app stop another's loop."""
    module = __import__(module_name, fromlist=["celery_app"])
    calls: list[str] = []
    monkeypatch.setattr(module, "start_shared_loop", lambda: calls.append("start"))
    monkeypatch.setattr(module, "initialize_redis_pool", lambda: calls.append("redis_start"))
    monkeypatch.setattr(module, "close_redis_pool", lambda: calls.append("redis_stop"))
    monkeypatch.setattr(module, "stop_shared_loop", lambda: calls.append("stop"))
    sender = SimpleNamespace(app=SimpleNamespace(main="another_worker"))

    module.worker_ready_handler(sender=sender)
    module.worker_shutdown_handler(sender=sender)

    assert calls == []


def test_deal_worker_does_not_enable_prefork_child_recycling() -> None:
    """Thread pools cannot honor child recycling, so the config must not claim it."""
    from src.workers.deal_worker.celery_app import app

    assert app.conf.worker_max_tasks_per_child is None
