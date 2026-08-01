"""Regression tests for the process-local Celery worker liveness heartbeat."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import Mock

from src.workers.base import celery_base


def test_readiness_app_does_not_import_full_worker_runtime() -> None:
    """Probe startup must avoid task, tenant, database, and Key Vault initialization."""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and static import check
        [
            sys.executable,
            "-c",
            "from src.workers.base.healthcheck_app import app; import sys; "
            "assert app.main == 'worker_healthcheck'; "
            "assert 'src.workers.base.celery_base' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_worker_heartbeat_creates_and_refreshes_timestamp(monkeypatch, tmp_path) -> None:
    """The parent timer must leave a fresh timestamp for Kubernetes liveness."""
    heartbeat_path = tmp_path / "celery-worker-heartbeat"
    monkeypatch.setattr(celery_base, "_WORKER_HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(celery_base, "_heartbeat_write_failed", False)

    celery_base._touch_worker_heartbeat()
    os.utime(heartbeat_path, (1, 1))
    celery_base._touch_worker_heartbeat()

    assert heartbeat_path.stat().st_mtime > 1


def test_worker_heartbeat_write_failure_does_not_escape_timer(monkeypatch, tmp_path) -> None:
    """A filesystem failure must not terminate Celery's parent event loop."""
    logger = Mock()
    heartbeat_path = tmp_path / "missing" / "celery-worker-heartbeat"
    monkeypatch.setattr(celery_base, "_WORKER_HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(celery_base, "_heartbeat_write_failed", False)
    monkeypatch.setattr(celery_base, "get_logger", lambda _name: logger)

    celery_base._touch_worker_heartbeat()
    celery_base._touch_worker_heartbeat()

    logger.exception.assert_called_once()
