from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_celery_task_id_var: ContextVar[str | None] = ContextVar("celery_task_id", default=None)
_celery_task_name_var: ContextVar[str | None] = ContextVar("celery_task_name", default=None)
_celery_queue_var: ContextVar[str | None] = ContextVar("celery_queue", default=None)


def generate_request_id() -> str:
    return uuid4().hex


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_request_id(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def clear_request_id() -> None:
    _request_id_var.set(None)


def get_celery_task_id() -> str | None:
    return _celery_task_id_var.get()


def get_celery_task_name() -> str | None:
    return _celery_task_name_var.get()


def get_celery_queue() -> str | None:
    return _celery_queue_var.get()


def set_celery_context(
    *,
    task_id: str | None = None,
    task_name: str | None = None,
    queue: str | None = None,
) -> None:
    _celery_task_id_var.set(task_id)
    _celery_task_name_var.set(task_name)
    _celery_queue_var.set(queue)


def clear_celery_context() -> None:
    _celery_task_id_var.set(None)
    _celery_task_name_var.set(None)
    _celery_queue_var.set(None)


def reset_context() -> None:
    clear_request_id()
    clear_celery_context()


def correlation_headers(extra: Mapping[str, Any] | None = None) -> dict[str, str] | None:
    """
    Build Celery task headers from the current request/task context.

    Returns None if there is nothing to attach (so callers can pass it directly
    into Celery APIs without changing behavior).
    """
    headers: dict[str, str] = {}

    request_id = get_request_id()
    if request_id:
        headers["request_id"] = request_id

    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            headers[str(key)] = str(value)

    return headers or None


def load_from_headers(headers: Mapping[str, Any] | None) -> None:
    """
    Load correlation context from Celery headers (or other message headers).
    """
    if not headers:
        return

    request_id = (
        headers.get("request_id") or headers.get("x-request-id") or headers.get("X-Request-Id")
    )
    if isinstance(request_id, str) and request_id:
        set_request_id(request_id)
