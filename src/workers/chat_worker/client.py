"""Compatibility wrapper for chat task dispatch."""

from src.infra.messaging.task_dispatcher import (
    get_task_client as get_chat_celery_client,
)
from src.infra.messaging.task_dispatcher import (
    send_task as send_chat_task,
)
from src.infra.messaging.task_dispatcher import (
    send_task_async as send_chat_task_async,
)

__all__ = [
    "get_chat_celery_client",
    "send_chat_task",
    "send_chat_task_async",
]
