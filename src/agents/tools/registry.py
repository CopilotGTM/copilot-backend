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
    """Canonical names for all available tools.

    Tools are grouped by category:
    - Search: Knowledge base and internet search
    - Deal: Deal lookup, listing, and details
    - Stakeholder: Stakeholder info and insights
    - Meeting: Meeting details, transcripts, summaries
    - Communication: Emails and notes
    - Research: External research (Wikipedia, stocks, etc.)
    - Calendar: Upcoming meetings and events
    """

    # -------------------------------------------------------------------------
    # SEARCH TOOLS
    # -------------------------------------------------------------------------
    SEARCH_INTERNAL_KNOWLEDGE = "search_internal_knowledge"
    SEARCH_INTERNET = "search_internet"

    # -------------------------------------------------------------------------
    # DEAL TOOLS - For deal lookup, listing, details, and summaries
    # Used by: Chat Agent, Deal Summary Agent
    # -------------------------------------------------------------------------
    SEARCH_DEALS = "search_deals"  # Quick lookup by name/company
    LIST_DEALS = "list_deals"  # List with filters (status, amount, etc.)
    GET_DEAL_DETAILS = "get_deal_details"  # Full deal info with stakeholders/risks
    GET_DEAL_SUMMARY = "get_deal_summary"  # AI-generated deal summary
    GET_DEAL_CHANGE_HISTORY = "get_deal_change_history"  # Stage transition history
    GET_PIPELINE_DETAILS = "get_pipeline_details"  # Pipeline and stage information
    CREATE_DEAL = "create_deal"  # Send interactive deal creation form to chat
    ADD_DEAL_NOTE = "add_deal_note"  # Send interactive note form for existing deal
    ADD_STAKEHOLDER = "add_stakeholder"  # Send interactive stakeholder form for existing deal
    UPDATE_DEAL_STAGE = "update_deal_stage"  # Send interactive stage update form

    # -------------------------------------------------------------------------
    # STAKEHOLDER TOOLS - For stakeholder info and insights
    # Used by: Chat Agent, Deal Agent, Briefing Agent
    # -------------------------------------------------------------------------
    LIST_DEAL_STAKEHOLDERS = "list_deal_stakeholders"
    GET_STAKEHOLDER_DETAILS = "get_stakeholder_details"
    LIST_DEAL_STAKEHOLDER_INSIGHTS = "list_deal_stakeholder_insights"
    UPSERT_STAKEHOLDER = "upsert_stakeholder"  # Create-or-update stakeholder by email
    ENRICH_STAKEHOLDER = "enrich_stakeholder"  # Enrich stakeholder with LinkedIn data
    GET_EXISTING_RISKS_BLOCKERS = "get_existing_risks_blockers"

    # -------------------------------------------------------------------------
    # MEETING TOOLS - For meeting details, transcripts, summaries
    # Used by: Chat Agent, Meeting Summary Agent, Briefing Agent
    # -------------------------------------------------------------------------
    LIST_DEAL_MEETINGS = "list_deal_meetings"  # List meetings for a specific deal
    LIST_MEETINGS_BY_DATE = "list_meetings_by_date"  # List meetings across all deals by date
    GET_MEETING_DETAILS = "get_meeting_details"  # Full meeting info
    GET_MEETING_SUMMARY = "get_meeting_summary"  # AI-generated meeting summary
    GET_MEETING_TRANSCRIPT = "get_meeting_transcript"
    GET_MEETING_DEAL_CONTEXT = "get_meeting_deal_context"

    # -------------------------------------------------------------------------
    # CONTACT TOOLS - For contact discovery across deals
    # Used by: Chat Agent
    # -------------------------------------------------------------------------
    SEARCH_CONTACTS = "search_contacts"  # Search contacts by name/email/company

    # -------------------------------------------------------------------------
    # COMMUNICATION TOOLS - For emails, messaging channels, and notes
    # Used by: Chat Agent, Deal Agent, Briefing Agent, Risk/Blocker Agent
    # -------------------------------------------------------------------------
    LIST_DEAL_EMAILS = "list_deal_emails"
    GET_EMAIL_DETAILS = "get_email_details"
    LIST_DEAL_NOTES = "list_deal_notes"
    LIST_DEAL_CHANNEL_MESSAGES = "list_deal_channel_messages"
    FETCH_TEAMS_CHAT_MESSAGES = "fetch_teams_chat_messages"
    CREATE_VISUALIZATION_CARD = "create_visualization_card"

    # -------------------------------------------------------------------------
    # RESEARCH TOOLS - External research
    # Used by: Chat Agent, Briefing Agent, Deal Summary Agent
    # -------------------------------------------------------------------------
    SEARCH_WIKIPEDIA = "search_wikipedia"
    GET_STOCK_INFO = "get_stock_info"
    GET_COMPANY_FINANCIALS = "get_company_financials"
    CRAWL_WEBPAGE = "crawl_webpage"
    SEARCH_ARXIV = "search_arxiv"
    EXTRACT_NEWS_ARTICLE = "extract_news_article"
    ENRICH_LINKEDIN_PROFILE = (
        "enrich_linkedin_profile"  # LinkedIn enrichment for stakeholder workflows
    )
    RESEARCH_LINKEDIN_PROFILE = "research_linkedin_profile"  # LinkedIn research for general chat

    # -------------------------------------------------------------------------
    # CALENDAR TOOLS - For upcoming meetings and events
    # Used by: Meeting Agent, Briefing Agent
    # -------------------------------------------------------------------------
    GET_UPCOMING_MEETINGS = "get_upcoming_meetings"
    GET_CALENDAR_EVENTS = "get_calendar_events"

    # -------------------------------------------------------------------------
    # BRIEFING/FOLLOW-UP TOOLS - For meeting prep and action items
    # Used by: Meeting Agent, Follow-Up Agent, Briefing Agent
    # -------------------------------------------------------------------------
    GET_BRIEFING = "get_briefing"
    GET_FOLLOW_UPS = "get_follow_ups"
    GET_ACTION_ITEMS = "get_action_items"


# =============================================================================
# CHANNEL-SPECIFIC TOOLS
# =============================================================================
# Tools that should ONLY be available on specific channels.
# Tools not listed here are available on all channels.
# =============================================================================

CHANNEL_ONLY_TOOLS: dict[ToolName, set[str]] = {
    ToolName.FETCH_TEAMS_CHAT_MESSAGES: {"teams"},
    ToolName.CREATE_VISUALIZATION_CARD: {"teams"},
    ToolName.CREATE_DEAL: {"teams"},
    ToolName.ADD_DEAL_NOTE: {"teams"},
    ToolName.ADD_STAKEHOLDER: {"teams"},
    ToolName.UPDATE_DEAL_STAGE: {"teams"},
}


# =============================================================================
# AGENT-TO-TOOLS MAPPING
# =============================================================================
# Maps each agent class to the tools it can access.
# All agents use get_tools_for_agent() to retrieve their tools from this mapping.
#
# IMPORTANT: Agent names must match the actual class names exactly.
# =============================================================================

AGENT_TOOLS: dict[str, set[ToolName]] = {
    # -------------------------------------------------------------------------
    # GeneralAgent - Chat Agent
    # Location: src/agents/workflows/general/agent.py
    # Purpose: Primary chat interface that handles user queries in the chat UI.
    # -------------------------------------------------------------------------
    "GeneralAgent": {
        # Search tools
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        ToolName.SEARCH_INTERNET,
        # Deal tools
        ToolName.SEARCH_DEALS,
        ToolName.LIST_DEALS,
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_DEAL_SUMMARY,
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS,
        ToolName.GET_EXISTING_RISKS_BLOCKERS,
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.LIST_MEETINGS_BY_DATE,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_SUMMARY,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_DEAL_CONTEXT,
        # Contact tools
        ToolName.SEARCH_CONTACTS,
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.LIST_DEAL_NOTES,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
        ToolName.CREATE_VISUALIZATION_CARD,  # Send visualization summary card
        # Research tools
        ToolName.SEARCH_WIKIPEDIA,
        ToolName.GET_STOCK_INFO,
        ToolName.GET_COMPANY_FINANCIALS,
        ToolName.CRAWL_WEBPAGE,
        ToolName.SEARCH_ARXIV,
        ToolName.EXTRACT_NEWS_ARTICLE,
        ToolName.RESEARCH_LINKEDIN_PROFILE,  # LinkedIn research for general queries
        # Teams tools
        ToolName.FETCH_TEAMS_CHAT_MESSAGES,  # Fetch group chat history when @mentioned
        # CRM write tools (channel-only)
        ToolName.CREATE_DEAL,  # Send interactive deal creation form
        ToolName.ADD_DEAL_NOTE,  # Send interactive note form for existing deal
        ToolName.ADD_STAKEHOLDER,  # Send interactive stakeholder form
        ToolName.UPDATE_DEAL_STAGE,  # Send interactive stage update form
    },
    # -------------------------------------------------------------------------
    # BriefingAgent - Meeting Briefing Agent
    # Location: src/agents/workflows/briefing/agent.py
    # Purpose: Generates comprehensive meeting prep briefings before calls.
    # -------------------------------------------------------------------------
    "BriefingAgent": {
        # Search tools
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        ToolName.SEARCH_INTERNET,
        # Deal tools (for understanding deal context)
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        ToolName.GET_EXISTING_RISKS_BLOCKERS,  # Know active risks/blockers before meeting
        # Meeting tools
        ToolName.GET_MEETING_DETAILS,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_SUMMARY,  # Past meeting summaries for quick context
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS,  # Understand attendee dynamics
        ToolName.UPSERT_STAKEHOLDER,  # Maintain stakeholders by email in one call
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
        ToolName.LIST_DEAL_NOTES,  # Internal notes contain prep context
    },
    # -------------------------------------------------------------------------
    # SummaryAgent - Meeting Summary Agent
    # Location: src/agents/workflows/summary/agent.py
    # Purpose: Generates AI summaries from meeting transcripts.
    # -------------------------------------------------------------------------
    "SummaryAgent": {
        # Meeting tools
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_DEAL_CONTEXT,
        ToolName.LIST_DEAL_MEETINGS,  # Reference prior meetings for context
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS,  # Understand stakeholder roles/concerns
        ToolName.UPSERT_STAKEHOLDER,  # Maintain stakeholders by email in one call
        # Risk context
        ToolName.GET_EXISTING_RISKS_BLOCKERS,  # Identify if discussed items are known risks
        ToolName.LIST_DEAL_NOTES,  # Notes may contain additional stakeholder mentions
    },
    # -------------------------------------------------------------------------
    # DealSummaryAgent - Deal Summary Agent
    # Location: src/agents/workflows/deal_summary/agent.py
    # Purpose: Generates comprehensive deal summaries from all deal data.
    # -------------------------------------------------------------------------
    "DealSummaryAgent": {
        # Deal tools
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        ToolName.LIST_DEAL_NOTES,
        ToolName.GET_EXISTING_RISKS_BLOCKERS,  # Deal health requires risks/blockers
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS,  # Crucial for deal analysis
        ToolName.UPSERT_STAKEHOLDER,  # Maintain stakeholders by email in one call
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_SUMMARY,  # Quick context from past meetings
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
    },
    # -------------------------------------------------------------------------
    # RiskBlockerExtractionAgent - Risk/Blocker Extraction Agent
    # Location: src/agents/workflows/risk_blocker_extraction/agent.py
    # Purpose: Extracts risks and blockers from meetings, emails, and notes.
    # -------------------------------------------------------------------------
    "RiskBlockerExtractionAgent": {
        # Risk/blocker tools
        ToolName.GET_EXISTING_RISKS_BLOCKERS,
        # Deal tools (for understanding deal progression context)
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        # Stakeholder tools (for risk attribution)
        ToolName.LIST_DEAL_STAKEHOLDERS,  # Stakeholders can be source of blockers
        ToolName.GET_STAKEHOLDER_DETAILS,  # Context for risk attribution
        ToolName.UPSERT_STAKEHOLDER,  # Maintain stakeholders by email in one call
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
        ToolName.LIST_DEAL_NOTES,  # Notes contain risk/blocker information
    },
    # -------------------------------------------------------------------------
    # StakeholderAnalysisAgent - Stakeholder Analysis Agent
    # Location: src/agents/workflows/stakeholder_analysis/agent.py
    # Purpose: Analyzes stakeholders and enriches their profiles with LinkedIn data.
    # -------------------------------------------------------------------------
    "StakeholderAnalysisAgent": {
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.GET_STAKEHOLDER_DETAILS,
        ToolName.UPSERT_STAKEHOLDER,
        ToolName.ENRICH_STAKEHOLDER,  # LinkedIn enrichment for stakeholders
        # Meeting tools (for context)
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_TRANSCRIPT,
    },
}

# Tool category mapping for documentation/grouping
TOOL_CATEGORIES: dict[ToolName, ToolCategory] = {
    # Search
    ToolName.SEARCH_INTERNAL_KNOWLEDGE: ToolCategory.SEARCH,
    ToolName.SEARCH_INTERNET: ToolCategory.SEARCH,
    # CRM/Deals
    ToolName.SEARCH_DEALS: ToolCategory.CRM,
    ToolName.LIST_DEALS: ToolCategory.CRM,
    ToolName.GET_DEAL_DETAILS: ToolCategory.CRM,
    ToolName.GET_DEAL_SUMMARY: ToolCategory.CRM,
    ToolName.GET_DEAL_CHANGE_HISTORY: ToolCategory.CRM,
    ToolName.GET_PIPELINE_DETAILS: ToolCategory.CRM,
    ToolName.CREATE_DEAL: ToolCategory.CRM,
    ToolName.ADD_DEAL_NOTE: ToolCategory.CRM,
    ToolName.ADD_STAKEHOLDER: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_STAGE: ToolCategory.CRM,
    # CRM/Stakeholder
    ToolName.LIST_DEAL_STAKEHOLDERS: ToolCategory.CRM,
    ToolName.GET_STAKEHOLDER_DETAILS: ToolCategory.CRM,
    ToolName.UPSERT_STAKEHOLDER: ToolCategory.CRM,
    ToolName.ENRICH_STAKEHOLDER: ToolCategory.CRM,
    ToolName.GET_EXISTING_RISKS_BLOCKERS: ToolCategory.CRM,
    ToolName.LIST_DEAL_NOTES: ToolCategory.CRM,
    ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS: ToolCategory.CRM,
    # Meeting Intelligence
    ToolName.LIST_DEAL_MEETINGS: ToolCategory.INTELLIGENCE,
    ToolName.LIST_MEETINGS_BY_DATE: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DETAILS: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_TRANSCRIPT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DEAL_CONTEXT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_SUMMARY: ToolCategory.INTELLIGENCE,
    # Contacts
    ToolName.SEARCH_CONTACTS: ToolCategory.CRM,
    ToolName.GET_BRIEFING: ToolCategory.INTELLIGENCE,
    ToolName.GET_FOLLOW_UPS: ToolCategory.INTELLIGENCE,
    ToolName.GET_ACTION_ITEMS: ToolCategory.INTELLIGENCE,
    # Calendar
    ToolName.GET_UPCOMING_MEETINGS: ToolCategory.CALENDAR,
    ToolName.GET_CALENDAR_EVENTS: ToolCategory.CALENDAR,
    # Communication
    ToolName.LIST_DEAL_EMAILS: ToolCategory.COMMUNICATION,
    ToolName.GET_EMAIL_DETAILS: ToolCategory.COMMUNICATION,
    ToolName.LIST_DEAL_CHANNEL_MESSAGES: ToolCategory.COMMUNICATION,
    ToolName.FETCH_TEAMS_CHAT_MESSAGES: ToolCategory.COMMUNICATION,
    ToolName.CREATE_VISUALIZATION_CARD: ToolCategory.COMMUNICATION,
    # Research tools
    ToolName.SEARCH_WIKIPEDIA: ToolCategory.SEARCH,
    ToolName.GET_STOCK_INFO: ToolCategory.SEARCH,
    ToolName.GET_COMPANY_FINANCIALS: ToolCategory.SEARCH,
    ToolName.CRAWL_WEBPAGE: ToolCategory.SEARCH,
    ToolName.SEARCH_ARXIV: ToolCategory.SEARCH,
    ToolName.EXTRACT_NEWS_ARTICLE: ToolCategory.SEARCH,
    ToolName.ENRICH_LINKEDIN_PROFILE: ToolCategory.SEARCH,
    ToolName.RESEARCH_LINKEDIN_PROFILE: ToolCategory.SEARCH,
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
            create_create_visualization_card,
            create_fetch_teams_chat_messages,
            create_get_email_details,
            create_list_deal_channel_messages,
            create_list_deal_emails,
        )
        from src.agents.tools.crm import (
            create_add_deal_note,
            create_add_stakeholder,
            create_create_deal,
            create_enrich_stakeholder,
            create_get_deal_change_history,
            create_get_deal_details,
            create_get_deal_summary,
            create_get_existing_risks_blockers,
            create_get_pipeline_details,
            create_get_stakeholder_details,
            create_list_deal_notes,
            create_list_deal_stakeholder_insights,
            create_list_deal_stakeholders,
            create_list_deals,
            create_search_contacts,
            create_search_deals,
            create_update_deal_stage,
            create_upsert_stakeholder,
        )
        from src.agents.tools.intelligence import (
            create_get_meeting_deal_context,
            create_get_meeting_details,
            create_get_meeting_summary,
            create_get_meeting_transcript,
            create_list_deal_meetings,
            create_list_meetings_by_date,
        )
        from src.agents.tools.research import (
            create_crawl_webpage,
            create_enrich_linkedin_profile,
            create_extract_news_article,
            create_get_company_financials,
            create_get_stock_info,
            create_research_linkedin_profile,
            create_search_arxiv,
            create_search_wikipedia,
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

        # CRM/Deal tools
        self._tool_factories[ToolName.SEARCH_DEALS] = lambda: create_search_deals(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_DEALS] = lambda: create_list_deals(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_DEAL_DETAILS] = lambda: create_get_deal_details(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_DEAL_SUMMARY] = lambda: create_get_deal_summary(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_DEAL_CHANGE_HISTORY] = (
            lambda: create_get_deal_change_history(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.GET_PIPELINE_DETAILS] = lambda: create_get_pipeline_details(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_DEAL] = lambda: create_create_deal(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.ADD_DEAL_NOTE] = lambda: create_add_deal_note(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.ADD_STAKEHOLDER] = lambda: create_add_stakeholder(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_DEAL_STAGE] = lambda: create_update_deal_stage(
            self.context, self.capabilities_getter
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
        self._tool_factories[ToolName.LIST_DEAL_NOTES] = lambda: create_list_deal_notes(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_DEAL_STAKEHOLDER_INSIGHTS] = (
            lambda: create_list_deal_stakeholder_insights(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.UPSERT_STAKEHOLDER] = lambda: create_upsert_stakeholder(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.ENRICH_STAKEHOLDER] = lambda: create_enrich_stakeholder(
            self.context, self.capabilities_getter
        )

        # Contact tools
        self._tool_factories[ToolName.SEARCH_CONTACTS] = lambda: create_search_contacts(
            self.context, self.capabilities_getter
        )

        # Meeting Intelligence tools
        self._tool_factories[ToolName.LIST_DEAL_MEETINGS] = lambda: create_list_deal_meetings(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_MEETINGS_BY_DATE] = lambda: create_list_meetings_by_date(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_MEETING_DETAILS] = lambda: create_get_meeting_details(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_MEETING_SUMMARY] = lambda: create_get_meeting_summary(
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
        self._tool_factories[ToolName.LIST_DEAL_CHANNEL_MESSAGES] = (
            lambda: create_list_deal_channel_messages(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.FETCH_TEAMS_CHAT_MESSAGES] = (
            lambda: create_fetch_teams_chat_messages(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.CREATE_VISUALIZATION_CARD] = (
            lambda: create_create_visualization_card(self.context, self.capabilities_getter)
        )

        # Research tools (free, no API keys - stateless, no context needed)
        self._tool_factories[ToolName.SEARCH_WIKIPEDIA] = lambda: create_search_wikipedia(
            self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_STOCK_INFO] = lambda: create_get_stock_info(
            self.capabilities_getter
        )
        self._tool_factories[ToolName.GET_COMPANY_FINANCIALS] = (
            lambda: create_get_company_financials(self.capabilities_getter)
        )
        self._tool_factories[ToolName.CRAWL_WEBPAGE] = lambda: create_crawl_webpage(
            self.capabilities_getter
        )
        self._tool_factories[ToolName.SEARCH_ARXIV] = lambda: create_search_arxiv(
            self.capabilities_getter
        )
        self._tool_factories[ToolName.EXTRACT_NEWS_ARTICLE] = lambda: create_extract_news_article(
            self.capabilities_getter
        )
        self._tool_factories[ToolName.ENRICH_LINKEDIN_PROFILE] = (
            lambda: create_enrich_linkedin_profile(self.capabilities_getter)
        )
        self._tool_factories[ToolName.RESEARCH_LINKEDIN_PROFILE] = (
            lambda: create_research_linkedin_profile(self.capabilities_getter)
        )

        # Legacy/placeholder tools - will be registered when implemented
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

    def get_tools_for_agent(self, agent_name: str, channel: str = "web") -> list[Callable]:
        """Get all tools allowed for a specific agent, filtered by channel.

        Args:
            agent_name: Name of the agent class (e.g., "GeneralAgent")
            channel: Channel identifier ("web", "teams"). Tools restricted to
                     other channels via CHANNEL_ONLY_TOOLS are excluded.

        Returns:
            List of tool functions the agent can use
        """
        allowed_tools = AGENT_TOOLS.get(agent_name, set())
        tools: list[Callable] = []

        for tool_name in allowed_tools:
            # Skip tools restricted to other channels
            allowed_channels = CHANNEL_ONLY_TOOLS.get(tool_name)
            if allowed_channels and channel not in allowed_channels:
                logger.debug(f"Skipping tool {tool_name} for {agent_name} (channel={channel})")
                continue

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

    Uses context.channel to filter channel-specific tools automatically.

    Args:
        agent_name: Name of the agent class
        context: AgentContext with session and org metadata
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        List of tool functions the agent can use
    """
    registry = ToolRegistry(context, capabilities_getter)
    return registry.get_tools_for_agent(agent_name, channel=context.channel)


__all__ = [
    "ToolCategory",
    "ToolName",
    "ToolRegistry",
    "AGENT_TOOLS",
    "TOOL_CATEGORIES",
    "get_tools_for_agent",
]
