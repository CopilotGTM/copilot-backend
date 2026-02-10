"""CRM routers package."""

from .custom_fields_handler import custom_fields_router
from .deal_import_handler import deal_import_router
from .deal_notes_handler import deal_notes_router
from .deal_participants_handler import deal_participants_router
from .deal_timeline_handler import deal_timeline_router
from .deals_handler import deals_router
from .meetings_handler import meetings_router
from .pipelines_handler import pipelines_router
from .risks_blockers_handler import risks_blockers_router
from .settings_handler import settings_router
from .stakeholder_insights_handler import stakeholder_insights_router
from .stakeholders_handler import stakeholders_router
from .tasks_handler import tasks_router
from .users_handler import crm_users_router

__all__ = [
    "deal_notes_router",
    "deal_participants_router",
    "deal_import_router",
    "custom_fields_router",
    "deal_timeline_router",
    "deals_router",
    "meetings_router",
    "pipelines_router",
    "risks_blockers_router",
    "settings_router",
    "stakeholder_insights_router",
    "stakeholders_router",
    "tasks_router",
    "crm_users_router",
]
