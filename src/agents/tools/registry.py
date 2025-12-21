"""Tool registry for agent-specific tool selection.

This module provides a centralized registry that maps agents to their
allowed tools, enabling fine-grained control over which capabilities
each agent can access.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    """Categories of tools for grouping and access control."""

    SEARCH = "search"
    CRM = "crm"
    INTELLIGENCE = "intelligence"
    CALENDAR = "calendar"
    COMMUNICATION = "communication"


class ToolName(str, Enum):
    """Canonical names for all available tools."""

    # Search tools
    SEARCH_INTERNAL_KNOWLEDGE = "search_internal_knowledge"
    SEARCH_INTERNET = "search_internet"

    # CRM/Stakeholder tools
    LIST_DEAL_STAKEHOLDERS = "list_deal_stakeholders"
    GET_STAKEHOLDER_DETAILS = "get_stakeholder_details"
    GET_EXISTING_RISKS_BLOCKERS = "get_existing_risks_blockers"

    # Meeting Intelligence tools
    LIST_DEAL_MEETINGS = "list_deal_meetings"
    GET_MEETING_DETAILS = "get_meeting_details"
    GET_MEETING_TRANSCRIPT = "get_meeting_transcript"
    GET_MEETING_DEAL_CONTEXT = "get_meeting_deal_context"

    # Communication/Email tools
    LIST_DEAL_EMAILS = "list_deal_emails"
    GET_EMAIL_DETAILS = "get_email_details"

    # Legacy placeholders (to be implemented or removed)
    GET_DEALS = "get_deals"
    GET_DEAL_DETAILS = "get_deal_details"
    GET_DEAL_STAGES = "get_deal_stages"
    GET_CONTACTS = "get_contacts"
    GET_STAKEHOLDERS = "get_stakeholders"  # Deprecated, use LIST_DEAL_STAKEHOLDERS

    # Intelligence tools (legacy placeholders)
    GET_MEETING_SUMMARY = "get_meeting_summary"  # Deprecated, use GET_MEETING_DETAILS
    GET_TRANSCRIPT = "get_transcript"  # Deprecated, use GET_MEETING_TRANSCRIPT
    GET_BRIEFING = "get_briefing"
    GET_FOLLOW_UPS = "get_follow_ups"
    GET_ACTION_ITEMS = "get_action_items"

    # Calendar tools (to be implemented)
    GET_UPCOMING_MEETINGS = "get_upcoming_meetings"
    GET_CALENDAR_EVENTS = "get_calendar_events"


# Agent-to-tools mapping
# Each agent type maps to a set of tool names it can access
AGENT_TOOLS: dict[str, set[ToolName]] = {
    "GeneralAgent": {
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        ToolName.SEARCH_INTERNET,
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
    },
    "DealAgent": {
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.GET_DEALS,
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_DEAL_STAGES,
        ToolName.GET_CONTACTS,
    },
    "MeetingAgent": {
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_BRIEFING,
        ToolName.GET_UPCOMING_MEETINGS,
    },
    "FollowUpAgent": {
        ToolName.GET_DEALS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_FOLLOW_UPS,
        ToolName.GET_ACTION_ITEMS,
    },
    "BriefingAgent": {
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.GET_DEALS,
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_BRIEFING,
        ToolName.GET_UPCOMING_MEETINGS,
    },
    "RiskBlockerExtractionAgent": {
        ToolName.GET_EXISTING_RISKS_BLOCKERS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
    },
}

# Tool category mapping for documentation/grouping
TOOL_CATEGORIES: dict[ToolName, ToolCategory] = {
    # Search
    ToolName.SEARCH_INTERNAL_KNOWLEDGE: ToolCategory.SEARCH,
    ToolName.SEARCH_INTERNET: ToolCategory.SEARCH,
    # CRM/Stakeholder
    ToolName.LIST_DEAL_STAKEHOLDERS: ToolCategory.CRM,
    ToolName.GET_STAKEHOLDER_DETAILS: ToolCategory.CRM,
    ToolName.GET_DEALS: ToolCategory.CRM,
    ToolName.GET_DEAL_DETAILS: ToolCategory.CRM,
    ToolName.GET_DEAL_STAGES: ToolCategory.CRM,
    ToolName.GET_CONTACTS: ToolCategory.CRM,
    ToolName.GET_STAKEHOLDERS: ToolCategory.CRM,
    ToolName.GET_EXISTING_RISKS_BLOCKERS: ToolCategory.CRM,
    # Meeting Intelligence
    ToolName.LIST_DEAL_MEETINGS: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DETAILS: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_TRANSCRIPT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DEAL_CONTEXT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_SUMMARY: ToolCategory.INTELLIGENCE,
    ToolName.GET_TRANSCRIPT: ToolCategory.INTELLIGENCE,
    ToolName.GET_BRIEFING: ToolCategory.INTELLIGENCE,
    ToolName.GET_FOLLOW_UPS: ToolCategory.INTELLIGENCE,
    ToolName.GET_ACTION_ITEMS: ToolCategory.INTELLIGENCE,
    # Calendar
    ToolName.GET_UPCOMING_MEETINGS: ToolCategory.CALENDAR,
    ToolName.GET_CALENDAR_EVENTS: ToolCategory.CALENDAR,
    # Communication
    ToolName.LIST_DEAL_EMAILS: ToolCategory.COMMUNICATION,
    ToolName.GET_EMAIL_DETAILS: ToolCategory.COMMUNICATION,
}


class ToolRegistry:
    """Registry for creating and managing agent tools.

    Provides factory methods to create tool functions bound to a specific
    context and capabilities, with agent-specific access control.
    """

    def __init__(
        self,
        context: AgentContext,
        capabilities_getter: Callable[[], AgentCapabilities | None],
    ):
        """Initialize registry with context and capabilities.

        Args:
            context: AgentContext with session and org metadata
            capabilities_getter: Callable that returns current AgentCapabilities
        """
        self.context = context
        self.capabilities_getter = capabilities_getter
        self._tool_factories: dict[ToolName, Callable[[], Callable]] = {}
        self._register_tool_factories()

    def _register_tool_factories(self) -> None:
        """Register all available tool factories."""
        # Import here to avoid circular imports
        from src.agents.tools.communication import (
            create_get_email_details,
            create_list_deal_emails,
        )
        from src.agents.tools.crm import (
            create_get_existing_risks_blockers,
            create_get_stakeholder_details,
            create_list_deal_stakeholders,
        )
        from src.agents.tools.intelligence import (
            create_get_meeting_deal_context,
            create_get_meeting_details,
            create_get_meeting_transcript,
            create_list_deal_meetings,
        )
        from src.agents.tools.search import (
            create_search_internal_knowledge,
            create_search_internet,
        )

        # Search tools
        self._tool_factories[ToolName.SEARCH_INTERNAL_KNOWLEDGE] = (
            lambda: create_search_internal_knowledge(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.SEARCH_INTERNET] = lambda: create_search_internet(
            self.capabilities_getter
        )

        # CRM/Stakeholder tools
        self._tool_factories[ToolName.LIST_DEAL_STAKEHOLDERS] = (
            lambda: create_list_deal_stakeholders(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.GET_STAKEHOLDER_DETAILS] = (
            lambda: create_get_stakeholder_details(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.GET_EXISTING_RISKS_BLOCKERS] = (
            lambda: create_get_existing_risks_blockers(self.context, self.capabilities_getter)
        )

        # Meeting Intelligence tools
        self._tool_factories[ToolName.LIST_DEAL_MEETINGS] = lambda: create_list_deal_meetings(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_MEETING_DETAILS] = lambda: create_get_meeting_details(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_MEETING_TRANSCRIPT] = (
            lambda: create_get_meeting_transcript(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.GET_MEETING_DEAL_CONTEXT] = (
            lambda: create_get_meeting_deal_context(self.context, self.capabilities_getter)
        )

        # Communication/Email tools
        self._tool_factories[ToolName.LIST_DEAL_EMAILS] = lambda: create_list_deal_emails(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_EMAIL_DETAILS] = lambda: create_get_email_details(
            self.context, self.capabilities_getter
        )

        # Legacy/placeholder tools - will be registered when implemented
        # self._tool_factories[ToolName.GET_DEALS] = ...
        # self._tool_factories[ToolName.GET_BRIEFING] = ...
        # self._tool_factories[ToolName.GET_UPCOMING_MEETINGS] = ...

    def get_tool(self, tool_name: ToolName) -> Callable | None:
        """Get a single tool by name.

        Args:
            tool_name: The tool to retrieve

        Returns:
            Tool function or None if not registered
        """
        factory = self._tool_factories.get(tool_name)
        if factory:
            return factory()
        logger.warning(f"Tool {tool_name} not registered")
        return None

    def get_tools_for_agent(self, agent_name: str) -> list[Callable]:
        """Get all tools allowed for a specific agent.

        Args:
            agent_name: Name of the agent class (e.g., "GeneralAgent")

        Returns:
            List of tool functions the agent can use
        """
        allowed_tools = AGENT_TOOLS.get(agent_name, set())
        tools: list[Callable] = []

        for tool_name in allowed_tools:
            tool = self.get_tool(tool_name)
            if tool:
                tools.append(tool)
                logger.debug(f"Registered tool {tool_name} for {agent_name}")
            else:
                logger.warning(f"Tool {tool_name} requested by {agent_name} but not available")

        return tools

    def get_tools_by_category(self, category: ToolCategory) -> list[Callable]:
        """Get all tools in a specific category.

        Args:
            category: The category to filter by

        Returns:
            List of tool functions in the category
        """
        tools: list[Callable] = []

        for tool_name, tool_category in TOOL_CATEGORIES.items():
            if tool_category == category:
                tool = self.get_tool(tool_name)
                if tool:
                    tools.append(tool)

        return tools

    def get_tools(self, tool_names: list[ToolName]) -> list[Callable]:
        """Get specific tools by name.

        Args:
            tool_names: List of tool names to retrieve

        Returns:
            List of tool functions
        """
        tools: list[Callable] = []

        for tool_name in tool_names:
            tool = self.get_tool(tool_name)
            if tool:
                tools.append(tool)

        return tools

    @classmethod
    def get_available_tools_for_agent(cls, agent_name: str) -> set[ToolName]:
        """Get the set of tool names available for an agent (static lookup).

        Args:
            agent_name: Name of the agent class

        Returns:
            Set of tool names the agent can use
        """
        return AGENT_TOOLS.get(agent_name, set())


def get_tools_for_agent(
    agent_name: str,
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> list[Callable]:
    """Convenience function to get tools for an agent.

    Args:
        agent_name: Name of the agent class
        context: AgentContext with session and org metadata
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        List of tool functions the agent can use
    """
    registry = ToolRegistry(context, capabilities_getter)
    return registry.get_tools_for_agent(agent_name)


__all__ = [
    "ToolCategory",
    "ToolName",
    "ToolRegistry",
    "AGENT_TOOLS",
    "TOOL_CATEGORIES",
    "get_tools_for_agent",
]
