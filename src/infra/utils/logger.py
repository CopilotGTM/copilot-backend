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
    "color_message",
}
_CONSOLE_IGNORED_EXTRA_FIELDS = {"event", "color_message", "asctime", "message"}
_CONSOLE_EXTRA_ALIASES = {
    "http_method": "method",
    "http_path": "path",
    "http_status_code": "status",
}
_CONSOLE_EXTRA_PRIORITY = [
    "http_method",
    "http_path",
    "http_status_code",
    "duration_ms",
    "organisation_id",
    "user_id",
    "deal_id",
    "stakeholder_id",
    "job_id",
    "integration_id",
]


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

        record.ctx_trace_id = trace_id
        record.ctx_span_id = span_id
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
    """Structured JSON formatter for production / log aggregators."""

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


_LEVEL_COLORS = {
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[32m",  # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "CRITICAL": "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


def _stringify_console_value(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\n", "\\n")
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json_dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)


def _iter_console_extra_pairs(record: logging.LogRecord) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for source_key, display_key in [
        ("ctx_celery_task_name", "celery_task"),
        ("ctx_celery_task_id", "celery_task_id"),
        ("ctx_celery_queue", "celery_queue"),
    ]:
        value = getattr(record, source_key, "")
        if value:
            pairs.append((display_key, _stringify_console_value(value)))
            seen_keys.add(source_key)

    extra_values: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_LOG_RECORD_FIELDS or key in _EXCLUDED_EXTRA_FIELDS:
            continue
        if key in _CONSOLE_IGNORED_EXTRA_FIELDS or key in seen_keys:
            continue
        if key.startswith("_") or value is None or value == "":
            continue
        extra_values[key] = value

    ordered_keys = [key for key in _CONSOLE_EXTRA_PRIORITY if key in extra_values]
    ordered_keys.extend(sorted(key for key in extra_values if key not in _CONSOLE_EXTRA_PRIORITY))

    for key in ordered_keys:
        display_key = _CONSOLE_EXTRA_ALIASES.get(key, key)
        pairs.append((display_key, _stringify_console_value(extra_values[key])))

    return pairs


class _ConsoleFormatter(logging.Formatter):
    """Human-readable colored formatter for local development."""

    def __init__(self, *, component: str | None = None):
        super().__init__()
        self._component = component

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - matches logging API
        color = _LEVEL_COLORS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S.%f")[:-3]
        level = f"{color}{record.levelname:<7}{_RESET}"

        # Preserve uvicorn logger names so `uvicorn.error` does not look like an actual error level.
        short_name = (
            record.name if record.name.startswith("uvicorn.") else record.name.rsplit(".", 1)[-1]
        )

        # request_id helps follow a single request across log lines;
        # process name distinguishes concurrent uvicorn workers
        req_id = getattr(record, "ctx_request_id", "")
        pid_tag = f"P{record.process}" if record.process else ""
        prefix = " ".join(filter(None, [pid_tag, req_id[:8] if req_id else ""]))
        prefix_str = f" ({prefix})" if prefix else ""

        extra_pairs = _iter_console_extra_pairs(record)
        extra_suffix = ""
        if extra_pairs:
            extra_suffix = " " + " ".join(f"{key}={value}" for key, value in extra_pairs)

        parts = [f"{ts} {level} [{short_name}]{prefix_str} {record.getMessage()}{extra_suffix}"]

        if record.exc_info:
            parts.append(self.formatException(record.exc_info))

        return "\n".join(parts)


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
        "celery.worker.consumer.connection",
        "celery.worker.consumer.mingle",
        "celery.worker.consumer.control",
        "celery.worker.control",
        "celery.apps.worker",
        "kombu.connection",
        "kombu.pidbox",
        "alembic.runtime.plugins",
        "alembic.runtime.migration",
        "celery.worker.strategy",
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
        env = os.getenv("ENV", "dev")
        formatter: logging.Formatter = (
            _ConsoleFormatter(component=component)
            if env == "local"
            else _JsonFormatter(component=component)
        )

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
