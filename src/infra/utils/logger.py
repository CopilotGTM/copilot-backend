import logging
import os
from datetime import UTC, datetime
from json import dumps as json_dumps

from src.infra.observability.context import (
    get_celery_queue,
    get_celery_task_id,
    get_celery_task_name,
    get_request_id,
)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - matches logging API
        record.request_id = get_request_id() or "-"
        record.celery_task_id = get_celery_task_id() or "-"
        record.celery_task_name = get_celery_task_name() or "-"
        record.celery_queue = get_celery_queue() or "-"
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - matches logging API
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "celery_task_id": getattr(record, "celery_task_id", None),
            "celery_task_name": getattr(record, "celery_task_name", None),
            "celery_queue": getattr(record, "celery_queue", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json_dumps(payload, ensure_ascii=False, separators=(",", ":"))


def get_logger(
    logger_name: str = "auth_logger",
    log_level: int | None = None,
    log_format: str = (
        "%(asctime)s - %(name)s - %(levelname)s - request_id=%(request_id)s "
        "celery_task=%(celery_task_name)s(%(celery_task_id)s) queue=%(celery_queue)s - %(message)s"
    ),
    log_file: str | None = None,
    rotate_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configure and return a logger with customizable settings.

    Args:
        logger_name (str): Name of the logger
        log_level (int): Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format (str): Format string for log messages
        log_file (Optional[str]): Path to log file. If None, logs to console only
        rotate_logs (bool): Whether to use rotating file handler
        max_bytes (int): Maximum size of log file before rotation
        backup_count (int): Number of backup files to keep

    Returns:
        logging.Logger: Configured logger instance
    """
    logger = logging.getLogger(logger_name)

    # Clear existing handlers to prevent duplicates
    if logger.hasHandlers():
        logger.handlers.clear()

    env_level = os.getenv("LOG_LEVEL")
    resolved_level = (
        getattr(logging, env_level.upper(), logging.INFO) if env_level else logging.INFO
    )
    if log_level is not None:
        resolved_level = log_level

    logger.setLevel(resolved_level)
    logger.propagate = False

    log_format_env = (os.getenv("LOG_FORMAT") or "").strip().lower()
    formatter: logging.Formatter = (
        _JsonFormatter() if log_format_env == "json" else logging.Formatter(log_format)
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_ContextFilter())
    logger.addHandler(console_handler)

    # File handler (if log_file is specified)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler: logging.Handler
        if rotate_logs:
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count
            )
        else:
            file_handler = logging.FileHandler(log_file)

        file_handler.setFormatter(formatter)
        file_handler.addFilter(_ContextFilter())
        logger.addHandler(file_handler)

    # Reduce noisy third-party loggers once (idempotent)
    for noisy in [
        "sqlalchemy.engine",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "urllib3.connectionpool",
    ]:
        nlog = logging.getLogger(noisy)
        if nlog.level == logging.NOTSET or nlog.level < logging.WARNING:
            nlog.setLevel(logging.WARNING)

    return logger
