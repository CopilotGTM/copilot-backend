from __future__ import annotations

from threading import Lock
from typing import Any

from opentelemetry import metrics

# Instruments are created lazily on first use so they bind to the MeterProvider
# installed by init_otel() rather than the noop provider present at import time.
_HTTP_SERVER_REQUEST_COUNT: Any = None
_HTTP_SERVER_REQUEST_DURATION: Any = None
_CELERY_TASK_EXECUTION_COUNT: Any = None
_CELERY_TASK_DURATION: Any = None

_init_lock = Lock()


def _ensure_http_instruments() -> None:
    global _HTTP_SERVER_REQUEST_COUNT, _HTTP_SERVER_REQUEST_DURATION
    if _HTTP_SERVER_REQUEST_COUNT is not None:
        return
    with _init_lock:
        if _HTTP_SERVER_REQUEST_COUNT is not None:
            return
        # Single meter namespace so dashboards and SLO queries discover all
        # first-party runtime metrics without per-module registration drift.
        meter = metrics.get_meter("copilotgtm.observability")
        _HTTP_SERVER_REQUEST_COUNT = meter.create_counter(
            name="copilotgtm.http.server.request.count",
            description="Total HTTP requests handled by the API.",
            unit="{request}",
        )
        _HTTP_SERVER_REQUEST_DURATION = meter.create_histogram(
            name="copilotgtm.http.server.request.duration",
            description="HTTP request duration in milliseconds.",
            unit="ms",
        )


def _ensure_celery_instruments() -> None:
    global _CELERY_TASK_EXECUTION_COUNT, _CELERY_TASK_DURATION
    if _CELERY_TASK_EXECUTION_COUNT is not None:
        return
    with _init_lock:
        if _CELERY_TASK_EXECUTION_COUNT is not None:
            return
        meter = metrics.get_meter("copilotgtm.observability")
        _CELERY_TASK_EXECUTION_COUNT = meter.create_counter(
            name="copilotgtm.celery.task.execution.count",
            description="Total Celery task executions by outcome.",
            unit="{task}",
        )
        _CELERY_TASK_DURATION = meter.create_histogram(
            name="copilotgtm.celery.task.duration",
            description="Celery task execution duration in milliseconds.",
            unit="ms",
        )


def _status_family(status_code: int) -> str:
    return f"{status_code // 100}xx"


def record_http_server_request(
    *,
    method: str,
    route: str | None,
    status_code: int,
    duration_ms: int,
) -> None:
    _ensure_http_instruments()
    # Route templates keep labels stable across UUID-heavy REST paths.
    attributes = {
        "http.method": (method or "UNKNOWN").upper(),
        "http.route": route or "unknown",
        "http.response.status_code": str(status_code),
        "http.status_family": _status_family(status_code),
    }
    _HTTP_SERVER_REQUEST_COUNT.add(1, attributes)
    _HTTP_SERVER_REQUEST_DURATION.record(duration_ms, attributes)


def record_celery_task_run(
    *,
    task_name: str | None,
    queue: str | None,
    status: str | None,
    duration_ms: int | None,
) -> None:
    _ensure_celery_instruments()
    # Queue/task labels are the main drill-down dimensions for worker SLOs and incident triage.
    attributes = {
        "celery.task_name": task_name or "unknown",
        "celery.queue": queue or "unknown",
        "celery.status": (status or "UNKNOWN").upper(),
    }
    _CELERY_TASK_EXECUTION_COUNT.add(1, attributes)
    if duration_ms is not None:
        # Some Celery flows may not produce a start timestamp; count them without fabricating latency.
        _CELERY_TASK_DURATION.record(duration_ms, attributes)


__all__ = ["record_celery_task_run", "record_http_server_request"]
