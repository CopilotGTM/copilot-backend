"""Task tools for creating tasks via chat (Slack/Teams).

These tools are chat-channel-only (Teams + Slack). They allow the GeneralAgent
to create CRM tasks from natural language. Created tasks go through a preview
card confirmation flow before being persisted.
"""

from src.agents.tools.tasks.create_task import create_create_task

__all__ = [
    "create_create_task",
]
