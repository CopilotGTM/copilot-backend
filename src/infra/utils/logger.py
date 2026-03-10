import inspect
import logging
import os
import sys
from datetime import UTC, datetime
from json import dumps as json_dumps
from logging.handlers import RotatingFileHandler
from typing import Any

from src.infra.observability.context import (
    get_celery_queue,
    get_celery_task_id,
    get_celery_task_name,
    get_request_id,
)

_ROOT_HANDLER_MARKER = "_copilot_root_logging_handler"
_LOGGER_FILE_HANDLER_MARKER = "_copilot_logger_file_handler"
_logging_configured = False
_configured_component: str | None = None

_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)
_EXCLUDED_EXTRA_FIELDS = {
    "ctx_request_id",
    "ctx_celery_task_id",
    "ctx_celery_task_name",
    "ctx_celery_queue",
    "ctx_trace_id",
    "ctx_span_id",
}


class _ContextFilter(logging.Filter):
    """Inject request, task, and trace context into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - matches logging API
        record.ctx_request_id = get_request_id() or ""  # type: ignore[attr-defined]
        record.ctx_celery_task_id = get_celery_task_id() or ""  # type: ignore[attr-defined]
        record.ctx_celery_task_name = get_celery_task_name() or ""  # type: ignore[attr-defined]
        record.ctx_celery_queue = get_celery_queue() or ""  # type: ignore[attr-defined]

        trace_id = ""
        span_id = ""
        try:
            from opentelemetry import trace as otel_trace

            span = otel_trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                trace_id = format(ctx.trace_id, "032x")
                span_id = format(ctx.span_id, "016x")
        except Exception:
            trace_id = ""
            span_id = ""

        record.ctx_trace_id = trace_id  # type: ignore[attr-defined]
        record.ctx_span_id = span_id  # type: ignore[attr-defined]
        return True


_CONTEXT_FIELDS: list[tuple[str, str]] = [
    ("ctx_trace_id", "trace_id"),
    ("ctx_span_id", "span_id"),
    ("ctx_request_id", "request_id"),
    ("ctx_celery_task_name", "celery_task"),
    ("ctx_celery_task_id", "celery_task_id"),
    ("ctx_celery_queue", "celery_queue"),
]


class _JsonFormatter(logging.Formatter):
    """Structured formatter for both API and worker processes."""

    def __init__(self, *, component: str | None = None):
        super().__init__()
        self._component = component
        self._environment = os.getenv("ENV", "dev")

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - matches logging API
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "environment": self._environment,
            "process_id": record.process,
            "process_name": record.processName,
        }

        if self._component:
            payload["component"] = self._component

        for attr, key in _CONTEXT_FIELDS:
            value = getattr(record, attr, "")
            if value:
                payload[key] = value

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_FIELDS or key in _EXCLUDED_EXTRA_FIELDS:
                continue
            if key.startswith("_") or value is None:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json_dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _resolve_log_level(log_level: int | None) -> int:
    env_level = os.getenv("LOG_LEVEL")
    resolved_level = (
        getattr(logging, env_level.upper(), logging.INFO) if env_level else logging.INFO
    )
    if log_level is not None:
        resolved_level = log_level
    return resolved_level


def _build_handler(
    *,
    formatter: logging.Formatter,
    log_file: str | None = None,
    rotate_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Handler:
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handler: logging.Handler = (
            RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
            if rotate_logs
            else logging.FileHandler(log_file)
        )
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())
    return handler


def _remove_managed_root_handlers(root_logger: logging.Logger) -> None:
    for handler in list(root_logger.handlers):
        if getattr(handler, _ROOT_HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()


def _reduce_noisy_third_party_logs() -> None:
    for noisy in [
        "sqlalchemy.engine",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "urllib3.connectionpool",
        "httpx",
        "httpcore",
    ]:
        logger = logging.getLogger(noisy)
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def _normalize_framework_loggers() -> None:
    for logger_name, level in [
        ("uvicorn", logging.INFO),
        ("uvicorn.error", logging.INFO),
        ("uvicorn.access", logging.WARNING),
        ("uvicorn.asgi", logging.INFO),
    ]:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)


def configure_logging(
    *,
    component: str | None = None,
    log_level: int | None = None,
    log_file: str | None = None,
    rotate_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    force: bool = False,
) -> None:
    """Configure a single root logger pipeline for the current process."""
    global _logging_configured, _configured_component

    resolved_level = _resolve_log_level(log_level)
    root_logger = logging.getLogger()
    should_reconfigure = (
        force
        or not _logging_configured
        or component != _configured_component
        or log_file is not None
    )

    if should_reconfigure:
        _remove_managed_root_handlers(root_logger)
        formatter = _JsonFormatter(component=component)

        console_handler = _build_handler(formatter=formatter)
        setattr(console_handler, _ROOT_HANDLER_MARKER, True)
        root_logger.addHandler(console_handler)

        if log_file:
            file_handler = _build_handler(
                formatter=formatter,
                log_file=log_file,
                rotate_logs=rotate_logs,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
            setattr(file_handler, _ROOT_HANDLER_MARKER, True)
            root_logger.addHandler(file_handler)

        _logging_configured = True
        _configured_component = component

    root_logger.setLevel(resolved_level)
    _reduce_noisy_third_party_logs()
    _normalize_framework_loggers()


def is_logging_configured() -> bool:
    return _logging_configured


def _resolve_logger_name(logger_name: str | None) -> str:
    if logger_name:
        return logger_name

    frame = inspect.currentframe()
    try:
        caller = frame.f_back if frame else None
        while caller:
            module_name = caller.f_globals.get("__name__")
            if module_name and module_name != __name__:
                return module_name
            caller = caller.f_back
    finally:
        del frame

    return "app"


def resolve_celery_component(base_component: str) -> str:
    """Differentiate worker and beat processes for the same Celery app module."""
    argv = {arg.lower() for arg in sys.argv}
    if "beat" in argv:
        return base_component.replace("_worker", "_beat")
    return base_component


def _attach_logger_file_handler(
    logger: logging.Logger,
    *,
    log_file: str,
    rotate_logs: bool,
    max_bytes: int,
    backup_count: int,
    component: str | None,
) -> None:
    for handler in logger.handlers:
        if (
            getattr(handler, _LOGGER_FILE_HANDLER_MARKER, False)
            and isinstance(handler, logging.FileHandler)
            and os.path.abspath(handler.baseFilename) == os.path.abspath(log_file)
        ):
            return

    formatter = _JsonFormatter(component=component or _configured_component)
    file_handler = _build_handler(
        formatter=formatter,
        log_file=log_file,
        rotate_logs=rotate_logs,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    setattr(file_handler, _LOGGER_FILE_HANDLER_MARKER, True)
    logger.addHandler(file_handler)


def get_logger(
    logger_name: str | None = None,
    log_level: int | None = None,
    log_file: str | None = None,
    rotate_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Return an application logger that propagates through the root pipeline."""
    if not is_logging_configured():
        configure_logging()

    logger = logging.getLogger(_resolve_logger_name(logger_name))
    logger.propagate = True

    if log_level is not None:
        logger.setLevel(log_level)

    if log_file:
        _attach_logger_file_handler(
            logger,
            log_file=log_file,
            rotate_logs=rotate_logs,
            max_bytes=max_bytes,
            backup_count=backup_count,
            component=_configured_component,
        )

    return logger
