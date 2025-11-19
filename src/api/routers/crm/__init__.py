"""CRM routers package."""

from .deals_handler import deals_router
from .tasks_handler import tasks_router

__all__ = ["deals_router", "tasks_router"]
