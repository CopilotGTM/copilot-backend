"""Task tools for chat-created tasks and deal-scoped task automation.

The existing `create_task` tool is chat-channel-only and uses a preview/confirm
flow. Deal task automation uses separate direct-write tools backed by
DealTaskAutomationService.
"""

# Re-export a complete surface for prompt catalogs and transparent tool loading.
from src.agents.tools.tasks.complete_deal_task import create_complete_deal_task
from src.agents.tools.tasks.create_deal_task import create_create_deal_task
from src.agents.tools.tasks.create_task import create_create_task
from src.agents.tools.tasks.list_deal_tasks import create_list_deal_tasks
from src.agents.tools.tasks.list_my_tasks import create_list_my_tasks
from src.agents.tools.tasks.update_task import create_update_task

# Keep tool factory names explicit for registries that import by attribute.
__all__ = [
    "create_complete_deal_task",
    "create_create_deal_task",
    "create_create_task",
    "create_list_deal_tasks",
    "create_list_my_tasks",
    "create_update_task",
]
