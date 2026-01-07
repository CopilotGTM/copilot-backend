"""CRM routers package."""

from .deal_notes_handler import deal_notes_router
from .deal_timeline_handler import deal_timeline_router
from .deals_handler import deals_router
from .meetings_handler import meetings_router
from .risks_blockers_handler import risks_blockers_router
from .stakeholder_insights_handler import stakeholder_insights_router
from .stakeholders_handler import stakeholders_router
from .tasks_handler import tasks_router

__all__ = [
    "deal_notes_router",
    "deal_timeline_router",
    "deals_router",
    "meetings_router",
    "risks_blockers_router",
    "stakeholder_insights_router",
    "stakeholders_router",
    "tasks_router",
]
