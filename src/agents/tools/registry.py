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
    REMINDERS = "reminders"


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
    GET_DEAL_MEMORY = "get_deal_memory"  # Persistent deal memory document
    GET_PIPELINE_DETAILS = "get_pipeline_details"  # Pipeline and stage information
    CREATE_DEAL = "create_deal"  # Create an internal CRM deal directly from chat
    LIST_LEADS = "list_leads"  # List leads with filters
    CREATE_LEAD = "create_lead"  # Create an internal CRM lead directly from chat
    CREATE_DRAFT_LEAD_FORM = "create_draft_lead_form"
    UPDATE_DRAFT_LEAD_FORM = "update_draft_lead_form"
    ADD_DEAL_NOTE = "add_deal_note"  # Create a note directly on an existing deal
    UPDATE_DEAL_NOTE = "update_deal_note"
    ADD_STAKEHOLDER = "add_stakeholder"  # Create a stakeholder directly on an existing deal
    UPDATE_DEAL_STAGE = "update_deal_stage"
    UPDATE_DEAL_FIELDS = "update_deal_fields"
    UPDATE_DEAL_LIFECYCLE = "update_deal_lifecycle"
    UPDATE_DEAL_ACCOUNT = "update_deal_account"  # Send interactive stage update form

    # -------------------------------------------------------------------------
    # STAKEHOLDER TOOLS - For stakeholder info and insights
    # Used by: Chat Agent, Deal Agent, Briefing Agent
    # -------------------------------------------------------------------------
    LIST_DEAL_STAKEHOLDERS = "list_deal_stakeholders"
    CREATE_STAKEHOLDER_IF_MISSING = "create_stakeholder_if_missing"
    GET_EXISTING_RISKS_BLOCKERS = "get_existing_risks_blockers"
    UPDATE_RISK_BLOCKER = "update_risk_blocker"
    DISMISS_RISK_BLOCKER = "dismiss_risk_blocker"
    RESOLVE_RISK_BLOCKER = "resolve_risk_blocker"

    # -------------------------------------------------------------------------
    # MEETING TOOLS - For meeting details, transcripts, summaries
    # Used by: Chat Agent, Meeting Summary Agent, Briefing Agent
    # -------------------------------------------------------------------------
    LIST_MEETINGS = "list_meetings"  # List current user's calendar meetings
    SEND_MEETING_BOT_INVITE = "send_meeting_bot_invite"  # Send recording bot to a meeting
    LIST_DEAL_MEETINGS = "list_deal_meetings"  # List meetings for a specific deal
    LIST_MEETINGS_BY_DATE = "list_meetings_by_date"  # List meetings across all deals by date
    GET_MEETING_DETAILS = "get_meeting_details"  # Full meeting info
    GET_MEETING_SUMMARY = "get_meeting_summary"  # AI-generated meeting summary
    GET_MEETING_TRANSCRIPT = "get_meeting_transcript"
    GET_COMPRESSED_TRANSCRIPT = "get_compressed_transcript"
    GET_MEETING_DEAL_CONTEXT = "get_meeting_deal_context"

    # -------------------------------------------------------------------------
    # CONTACT TOOLS - For contact discovery across deals
    # Used by: Chat Agent
    # -------------------------------------------------------------------------
    SEARCH_CONTACTS = "search_contacts"  # Search contacts by name/email/company
    SEARCH_ACCOUNTS = "search_accounts"  # Search accounts by name/domain
    CREATE_CONTACT = "create_contact"  # Create an internal CRM contact directly from chat
    CREATE_ACCOUNT = "create_account"  # Create an internal CRM account directly from chat
    UPDATE_CONTACT = "update_contact"  # Update a known internal CRM contact
    UPDATE_ACCOUNT = "update_account"  # Update a known internal CRM account

    # -------------------------------------------------------------------------
    # COMMUNICATION TOOLS - For emails, messaging channels, and notes
    # Used by: Chat Agent, Deal Agent, Briefing Agent, Risk/Blocker Agent
    # -------------------------------------------------------------------------
    LIST_DEAL_EMAILS = "list_deal_emails"
    GET_EMAIL_DETAILS = "get_email_details"
    READ_EMAIL_ATTACHMENT = "read_email_attachment"
    LIST_CONNECTED_EMAIL_ACCOUNTS = "list_connected_email_accounts"
    SEARCH_MAILBOX_EMAILS = "search_mailbox_emails"
    SEND_EMAIL = "send_email"
    SAVE_EMAIL_DRAFT = "save_email_draft"
    SEND_EMAIL_DRAFT = "send_email_draft"
    READ_CHAT_ATTACHMENT = "read_chat_attachment"
    LIST_CHAT_ATTACHMENTS = "list_chat_attachments"
    LIST_DEAL_NOTES = "list_deal_notes"
    LIST_DEAL_CHANNEL_MESSAGES = "list_deal_channel_messages"
    FETCH_TEAMS_CHAT_MESSAGES = "fetch_teams_chat_messages"

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
    RESEARCH_LINKEDIN_PROFILE = "research_linkedin_profile"  # LinkedIn research for general chat

    # -------------------------------------------------------------------------
    # CALENDAR TOOLS - For upcoming meetings and events
    # Used by: Meeting Agent, Briefing Agent
    # -------------------------------------------------------------------------
    GET_UPCOMING_MEETINGS = "get_upcoming_meetings"
    GET_CALENDAR_EVENTS = "get_calendar_events"
    SEND_CALENDAR_INVITE = "send_calendar_invite"
    UPDATE_CALENDAR_MEETING = "update_calendar_meeting"
    CANCEL_CALENDAR_MEETING = "cancel_calendar_meeting"

    # -------------------------------------------------------------------------
    # BRIEFING/FOLLOW-UP TOOLS - For meeting prep and action items
    # Used by: Meeting Agent, Follow-Up Agent, Briefing Agent
    # -------------------------------------------------------------------------
    GET_BRIEFING = "get_briefing"
    GET_FOLLOW_UPS = "get_follow_ups"
    GET_ACTION_ITEMS = "get_action_items"

    # -------------------------------------------------------------------------
    # TASK TOOLS - Chat task preview and deal-agent direct task automation
    # Used by: Chat Agent, DealTaskAgent
    # -------------------------------------------------------------------------
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    LIST_MY_TASKS = "list_my_tasks"
    LIST_DEAL_TASKS = "list_deal_tasks"
    CREATE_DEAL_TASK = "create_deal_task"
    COMPLETE_DEAL_TASK = "complete_deal_task"

    # -------------------------------------------------------------------------
    # REMINDER TOOLS - For scheduled reminders and agentic alerts
    # Used by: Chat Agent (Teams-channel-only)
    # -------------------------------------------------------------------------
    CREATE_REMINDER = "create_reminder"
    UPDATE_REMINDER = "update_reminder"
    LIST_REMINDERS = "list_reminders"
    CANCEL_REMINDER = "cancel_reminder"


REMINDER_MANAGEMENT_TOOLS = {
    ToolName.CREATE_REMINDER,
    ToolName.UPDATE_REMINDER,
    ToolName.LIST_REMINDERS,
    ToolName.CANCEL_REMINDER,
    ToolName.SEND_CALENDAR_INVITE,
    ToolName.UPDATE_CALENDAR_MEETING,
    ToolName.CANCEL_CALENDAR_MEETING,
    ToolName.SEND_EMAIL,
    ToolName.SAVE_EMAIL_DRAFT,
    ToolName.SEND_EMAIL_DRAFT,
}

VOICE_REALTIME_WRITE_EXCLUDED_TOOLS = {
    ToolName.UPDATE_DEAL_STAGE,
    ToolName.UPDATE_DEAL_FIELDS,
    ToolName.UPDATE_DEAL_LIFECYCLE,
    ToolName.UPDATE_DEAL_ACCOUNT,
    ToolName.UPDATE_CONTACT,
    ToolName.UPDATE_ACCOUNT,
    ToolName.CREATE_DRAFT_LEAD_FORM,
    ToolName.UPDATE_DRAFT_LEAD_FORM,
}

# WhatsApp payloads can contain forwarded text or attachment-derived content that
# looks imperative. Hide every mutating tool unless the current turn itself, or a
# short-lived explicit continuation, supplies authority for a side effect.
WHATSAPP_MUTATING_TOOLS = {
    ToolName.CREATE_DEAL,
    ToolName.CREATE_LEAD,
    ToolName.CREATE_DRAFT_LEAD_FORM,
    ToolName.UPDATE_DRAFT_LEAD_FORM,
    ToolName.ADD_DEAL_NOTE,
    ToolName.UPDATE_DEAL_NOTE,
    ToolName.ADD_STAKEHOLDER,
    ToolName.CREATE_STAKEHOLDER_IF_MISSING,
    ToolName.UPDATE_DEAL_STAGE,
    ToolName.UPDATE_DEAL_FIELDS,
    ToolName.UPDATE_DEAL_LIFECYCLE,
    ToolName.UPDATE_DEAL_ACCOUNT,
    ToolName.UPDATE_RISK_BLOCKER,
    ToolName.DISMISS_RISK_BLOCKER,
    ToolName.RESOLVE_RISK_BLOCKER,
    ToolName.SEND_MEETING_BOT_INVITE,
    ToolName.CREATE_CONTACT,
    ToolName.CREATE_ACCOUNT,
    ToolName.UPDATE_CONTACT,
    ToolName.UPDATE_ACCOUNT,
    ToolName.SEND_EMAIL,
    ToolName.SAVE_EMAIL_DRAFT,
    ToolName.SEND_EMAIL_DRAFT,
    ToolName.SEND_CALENDAR_INVITE,
    ToolName.UPDATE_CALENDAR_MEETING,
    ToolName.CANCEL_CALENDAR_MEETING,
    ToolName.CREATE_TASK,
    ToolName.UPDATE_TASK,
    ToolName.CREATE_DEAL_TASK,
    ToolName.COMPLETE_DEAL_TASK,
    ToolName.CREATE_REMINDER,
    ToolName.UPDATE_REMINDER,
    ToolName.CANCEL_REMINDER,
}


# =============================================================================
# CHANNEL-SPECIFIC TOOLS
# =============================================================================
# Tools that should ONLY be available on specific channels.
# Tools not listed here are available on all channels.
# =============================================================================

CHANNEL_ONLY_TOOLS: dict[ToolName, set[str]] = {
    ToolName.FETCH_TEAMS_CHAT_MESSAGES: {"teams"},
    ToolName.CREATE_DEAL: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CREATE_LEAD: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CREATE_DRAFT_LEAD_FORM: {"teams", "slack", "whatsapp", "web"},
    ToolName.UPDATE_DRAFT_LEAD_FORM: {"teams", "slack", "whatsapp", "web"},
    ToolName.CREATE_CONTACT: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CREATE_ACCOUNT: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_CONTACT: {"teams", "slack", "whatsapp", "web"},
    ToolName.UPDATE_ACCOUNT: {"teams", "slack", "whatsapp", "web"},
    ToolName.ADD_DEAL_NOTE: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_DEAL_NOTE: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.ADD_STAKEHOLDER: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_DEAL_STAGE: {"teams", "slack"},
    ToolName.CREATE_TASK: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_TASK: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CREATE_REMINDER: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_REMINDER: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.LIST_REMINDERS: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CANCEL_REMINDER: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.SEND_CALENDAR_INVITE: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.UPDATE_CALENDAR_MEETING: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.CANCEL_CALENDAR_MEETING: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.SEND_MEETING_BOT_INVITE: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.SEND_EMAIL: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.SAVE_EMAIL_DRAFT: {"teams", "slack", "whatsapp", "web", "voice"},
    ToolName.SEND_EMAIL_DRAFT: {"teams", "slack", "whatsapp", "web", "voice"},
}


# =============================================================================
# AGENT-TO-TOOLS MAPPING
# =============================================================================
# Maps each agent class to the tools it can access.
# All agents use get_tools_for_agent() to retrieve their tools from this mapping.
#
# IMPORTANT: Agent names must match the actual class names exactly.
# =============================================================================

GENERAL_AGENT_TOOLS: set[ToolName] = {
    # Search tools
    ToolName.SEARCH_INTERNAL_KNOWLEDGE,
    ToolName.SEARCH_INTERNET,
    # Deal tools
    ToolName.SEARCH_DEALS,
    ToolName.LIST_DEALS,
    ToolName.GET_DEAL_DETAILS,
    ToolName.GET_DEAL_SUMMARY,
    ToolName.GET_DEAL_MEMORY,
    ToolName.GET_DEAL_CHANGE_HISTORY,
    ToolName.GET_PIPELINE_DETAILS,
    # Stakeholder tools
    ToolName.LIST_DEAL_STAKEHOLDERS,
    ToolName.GET_EXISTING_RISKS_BLOCKERS,
    # Meeting tools
    ToolName.LIST_MEETINGS,
    ToolName.SEND_MEETING_BOT_INVITE,
    ToolName.GET_MEETING_DETAILS,
    ToolName.GET_MEETING_SUMMARY,
    ToolName.GET_MEETING_TRANSCRIPT,
    ToolName.GET_COMPRESSED_TRANSCRIPT,
    ToolName.GET_MEETING_DEAL_CONTEXT,
    # Contact tools
    ToolName.SEARCH_CONTACTS,
    ToolName.SEARCH_ACCOUNTS,
    ToolName.CREATE_CONTACT,
    ToolName.CREATE_ACCOUNT,
    ToolName.UPDATE_CONTACT,
    ToolName.UPDATE_ACCOUNT,
    # Communication tools
    ToolName.LIST_DEAL_EMAILS,
    ToolName.GET_EMAIL_DETAILS,
    ToolName.READ_EMAIL_ATTACHMENT,
    ToolName.LIST_CONNECTED_EMAIL_ACCOUNTS,
    ToolName.SEARCH_MAILBOX_EMAILS,
    ToolName.SEND_EMAIL,
    ToolName.SAVE_EMAIL_DRAFT,
    ToolName.SEND_EMAIL_DRAFT,
    ToolName.READ_CHAT_ATTACHMENT,
    ToolName.LIST_CHAT_ATTACHMENTS,
    ToolName.LIST_DEAL_NOTES,
    ToolName.LIST_DEAL_CHANNEL_MESSAGES,
    # Research tools
    ToolName.SEARCH_WIKIPEDIA,
    ToolName.GET_STOCK_INFO,
    ToolName.GET_COMPANY_FINANCIALS,
    ToolName.CRAWL_WEBPAGE,
    ToolName.SEARCH_ARXIV,
    ToolName.EXTRACT_NEWS_ARTICLE,
    ToolName.RESEARCH_LINKEDIN_PROFILE,
    # Teams tools
    ToolName.FETCH_TEAMS_CHAT_MESSAGES,
    # CRM write tools (channel-only)
    ToolName.CREATE_DEAL,
    ToolName.LIST_LEADS,
    ToolName.CREATE_LEAD,
    ToolName.CREATE_DRAFT_LEAD_FORM,
    ToolName.UPDATE_DRAFT_LEAD_FORM,
    ToolName.ADD_DEAL_NOTE,
    ToolName.UPDATE_DEAL_NOTE,
    ToolName.ADD_STAKEHOLDER,
    ToolName.UPDATE_DEAL_STAGE,
    ToolName.UPDATE_DEAL_FIELDS,
    ToolName.UPDATE_DEAL_LIFECYCLE,
    ToolName.UPDATE_DEAL_ACCOUNT,
    # Task tools
    ToolName.CREATE_TASK,
    ToolName.UPDATE_TASK,
    ToolName.LIST_MY_TASKS,
    ToolName.LIST_DEAL_TASKS,
    # Reminder tools (channel-only)
    ToolName.CREATE_REMINDER,
    ToolName.UPDATE_REMINDER,
    ToolName.LIST_REMINDERS,
    ToolName.CANCEL_REMINDER,
    # Calendar write tools (channel-only)
    ToolName.SEND_CALENDAR_INVITE,
    ToolName.UPDATE_CALENDAR_MEETING,
    ToolName.CANCEL_CALENDAR_MEETING,
}

AGENT_TOOLS: dict[str, set[ToolName]] = {
    # -------------------------------------------------------------------------
    # GeneralAgent - Chat Agent
    # Location: src/agents/workflows/general/agent.py
    # Purpose: Primary chat interface that handles user queries in the chat UI.
    # -------------------------------------------------------------------------
    "GeneralAgent": set(GENERAL_AGENT_TOOLS),
    # -------------------------------------------------------------------------
    # VoiceRealtimeAgent - Realtime voice workflow
    # Location: src/agents/workflows/voice_realtime/agent.py
    # Purpose: Voice-first realtime workflow that reuses the GeneralAgent tool profile.
    # -------------------------------------------------------------------------
    "VoiceRealtimeAgent": set(GENERAL_AGENT_TOOLS) - VOICE_REALTIME_WRITE_EXCLUDED_TOOLS,
    # BriefingAgent - Meeting Briefing Agent
    # Location: src/agents/workflows/briefing/agent.py
    # Purpose: Generates comprehensive meeting prep briefings before calls.
    # -------------------------------------------------------------------------
    "BriefingAgent": {
        # Search tools
        ToolName.SEARCH_INTERNET,
        ToolName.CRAWL_WEBPAGE,
        ToolName.RESEARCH_LINKEDIN_PROFILE,
        # Deal tools (for understanding deal context)
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_DEAL_MEMORY,  # Pre-built deal context for meeting prep
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
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.READ_CHAT_ATTACHMENT,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
        ToolName.LIST_DEAL_NOTES,  # Internal notes contain prep context
        ToolName.LIST_DEAL_TASKS,
    },
    # -------------------------------------------------------------------------
    # SummaryAgent - Meeting Summary Agent
    # Location: src/agents/workflows/summary/agent.py
    # Purpose: Generates AI summaries from meeting transcripts.
    # -------------------------------------------------------------------------
    "SummaryAgent": {
        # Meeting tools
        ToolName.GET_COMPRESSED_TRANSCRIPT,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_DEAL_CONTEXT,
        ToolName.LIST_DEAL_MEETINGS,  # Reference prior meetings for context
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.CREATE_STAKEHOLDER_IF_MISSING,  # Create net-new external stakeholders only
        ToolName.LIST_DEAL_NOTES,  # Notes may contain additional stakeholder mentions
        ToolName.RESEARCH_LINKEDIN_PROFILE,  # Fallback identity confirmation for discussed people
    },
    # -------------------------------------------------------------------------
    # DealSummaryAgent - Deal Summary Agent
    # Location: src/agents/workflows/deal_summary/agent.py
    # Purpose: Generates comprehensive deal summaries from all deal data.
    # -------------------------------------------------------------------------
    "DealSummaryAgent": {
        # Deal tools
        ToolName.GET_DEAL_MEMORY,  # Pre-built deal context as starting point
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        ToolName.LIST_DEAL_NOTES,
        ToolName.GET_EXISTING_RISKS_BLOCKERS,  # Deal health requires risks/blockers
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_TRANSCRIPT,
        ToolName.GET_MEETING_SUMMARY,  # Quick context from past meetings
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.READ_CHAT_ATTACHMENT,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
    },
    # -------------------------------------------------------------------------
    # RiskBlockerExtractionAgent - Risk/Blocker Extraction Agent
    # Location: src/agents/workflows/risk_blocker_extraction/agent.py
    # Purpose: Extracts risks and blockers from meetings, emails, and notes.
    # -------------------------------------------------------------------------
    "RiskBlockerExtractionAgent": {
        # Risk/blocker read + write tools
        ToolName.GET_EXISTING_RISKS_BLOCKERS,
        ToolName.UPDATE_RISK_BLOCKER,
        ToolName.DISMISS_RISK_BLOCKER,
        ToolName.RESOLVE_RISK_BLOCKER,
        # Deal context — deal memory is the primary input
        ToolName.GET_DEAL_MEMORY,
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        # Stakeholder context (for risk attribution)
        ToolName.LIST_DEAL_STAKEHOLDERS,
        # Meeting tools (targeted drill-down for quotes/evidence)
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_COMPRESSED_TRANSCRIPT,
        # Communication tools (targeted drill-down for quotes/evidence)
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.READ_CHAT_ATTACHMENT,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
        ToolName.LIST_DEAL_NOTES,
        # Knowledge base — verify technical risks against actual capabilities
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,
    },
    # -------------------------------------------------------------------------
    # StakeholderAnalysisAgent - Stakeholder Analysis Agent
    # Location: src/agents/workflows/stakeholder_analysis/agent.py
    # Purpose: Analyzes stakeholders and enriches their profiles with LinkedIn data.
    # -------------------------------------------------------------------------
    "StakeholderAnalysisAgent": {
        # Deal tools
        ToolName.GET_DEAL_MEMORY,  # Pre-built deal context as starting point
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.RESEARCH_LINKEDIN_PROFILE,  # LinkedIn research by name/URL
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_COMPRESSED_TRANSCRIPT,
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.READ_CHAT_ATTACHMENT,
        ToolName.LIST_DEAL_NOTES,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,
    },
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # DealUpdateAgent - Deal Update Agent
    # Location: src/agents/workflows/deal_update/agent.py
    # Purpose: Updates internal CRM deal fields through scoped write tools.
    # -------------------------------------------------------------------------
    "DealUpdateAgent": {
        ToolName.GET_DEAL_MEMORY,
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        ToolName.LIST_DEAL_STAKEHOLDERS,
        ToolName.LIST_DEAL_NOTES,
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_SUMMARY,
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.UPDATE_DEAL_FIELDS,
        ToolName.UPDATE_DEAL_LIFECYCLE,
        ToolName.UPDATE_DEAL_ACCOUNT,
    },
    # DealMemoryAgent - Deal Memory Agent
    # Location: src/agents/workflows/deal_memory/agent.py
    # Purpose: Builds and updates persistent deal memory documents.
    # -------------------------------------------------------------------------
    "DealMemoryAgent": {
        # Search tools
        ToolName.SEARCH_INTERNAL_KNOWLEDGE,  # Internal knowledge base
        ToolName.SEARCH_INTERNET,  # Company news, recent events
        # Deal tools
        ToolName.GET_DEAL_DETAILS,  # Current deal details for memory building
        ToolName.GET_DEAL_CHANGE_HISTORY,
        ToolName.GET_PIPELINE_DETAILS,
        ToolName.GET_DEAL_MEMORY,  # Retrieve existing deal memory for updates
        # Stakeholder tools
        ToolName.LIST_DEAL_STAKEHOLDERS,
        # Meeting tools
        ToolName.LIST_DEAL_MEETINGS,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_COMPRESSED_TRANSCRIPT,
        # Communication tools
        ToolName.LIST_DEAL_EMAILS,
        ToolName.GET_EMAIL_DETAILS,
        ToolName.READ_EMAIL_ATTACHMENT,
        ToolName.READ_CHAT_ATTACHMENT,
        ToolName.LIST_DEAL_NOTES,
        ToolName.LIST_DEAL_CHANNEL_MESSAGES,  # Slack/Teams channel messages
    },
    # -------------------------------------------------------------------------
    # DealTaskAgent - Post-meeting deal task automation
    # Location: src/agents/workflows/deal_tasks/agent.py
    # Purpose: Creates and completes internal deal tasks from meeting summaries.
    # -------------------------------------------------------------------------
    "DealTaskAgent": {
        ToolName.GET_DEAL_DETAILS,
        ToolName.GET_DEAL_MEMORY,
        ToolName.GET_MEETING_DETAILS,
        ToolName.GET_MEETING_SUMMARY,
        ToolName.GET_COMPRESSED_TRANSCRIPT,
        ToolName.LIST_DEAL_TASKS,
        ToolName.CREATE_DEAL_TASK,
        ToolName.COMPLETE_DEAL_TASK,
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
    ToolName.GET_DEAL_MEMORY: ToolCategory.CRM,
    ToolName.GET_PIPELINE_DETAILS: ToolCategory.CRM,
    ToolName.CREATE_DEAL: ToolCategory.CRM,
    ToolName.ADD_DEAL_NOTE: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_NOTE: ToolCategory.CRM,
    ToolName.ADD_STAKEHOLDER: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_STAGE: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_FIELDS: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_LIFECYCLE: ToolCategory.CRM,
    ToolName.UPDATE_DEAL_ACCOUNT: ToolCategory.CRM,
    # CRM/Stakeholder
    ToolName.LIST_DEAL_STAKEHOLDERS: ToolCategory.CRM,
    ToolName.CREATE_STAKEHOLDER_IF_MISSING: ToolCategory.CRM,
    ToolName.GET_EXISTING_RISKS_BLOCKERS: ToolCategory.CRM,
    ToolName.UPDATE_RISK_BLOCKER: ToolCategory.CRM,
    ToolName.DISMISS_RISK_BLOCKER: ToolCategory.CRM,
    ToolName.RESOLVE_RISK_BLOCKER: ToolCategory.CRM,
    ToolName.LIST_DEAL_NOTES: ToolCategory.CRM,
    # Meeting Intelligence
    ToolName.LIST_MEETINGS: ToolCategory.INTELLIGENCE,
    ToolName.SEND_MEETING_BOT_INVITE: ToolCategory.INTELLIGENCE,
    ToolName.LIST_DEAL_MEETINGS: ToolCategory.INTELLIGENCE,
    ToolName.LIST_MEETINGS_BY_DATE: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DETAILS: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_TRANSCRIPT: ToolCategory.INTELLIGENCE,
    ToolName.GET_COMPRESSED_TRANSCRIPT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_DEAL_CONTEXT: ToolCategory.INTELLIGENCE,
    ToolName.GET_MEETING_SUMMARY: ToolCategory.INTELLIGENCE,
    # Contacts
    ToolName.SEARCH_CONTACTS: ToolCategory.CRM,
    ToolName.SEARCH_ACCOUNTS: ToolCategory.CRM,
    ToolName.CREATE_CONTACT: ToolCategory.CRM,
    ToolName.CREATE_ACCOUNT: ToolCategory.CRM,
    ToolName.UPDATE_CONTACT: ToolCategory.CRM,
    ToolName.UPDATE_ACCOUNT: ToolCategory.CRM,
    ToolName.GET_BRIEFING: ToolCategory.INTELLIGENCE,
    ToolName.GET_FOLLOW_UPS: ToolCategory.INTELLIGENCE,
    ToolName.GET_ACTION_ITEMS: ToolCategory.INTELLIGENCE,
    # Calendar
    ToolName.GET_UPCOMING_MEETINGS: ToolCategory.CALENDAR,
    ToolName.GET_CALENDAR_EVENTS: ToolCategory.CALENDAR,
    ToolName.SEND_CALENDAR_INVITE: ToolCategory.CALENDAR,
    ToolName.UPDATE_CALENDAR_MEETING: ToolCategory.CALENDAR,
    ToolName.CANCEL_CALENDAR_MEETING: ToolCategory.CALENDAR,
    # Communication
    ToolName.LIST_DEAL_EMAILS: ToolCategory.COMMUNICATION,
    ToolName.GET_EMAIL_DETAILS: ToolCategory.COMMUNICATION,
    ToolName.READ_EMAIL_ATTACHMENT: ToolCategory.COMMUNICATION,
    ToolName.LIST_CONNECTED_EMAIL_ACCOUNTS: ToolCategory.COMMUNICATION,
    ToolName.SEARCH_MAILBOX_EMAILS: ToolCategory.COMMUNICATION,
    ToolName.SEND_EMAIL: ToolCategory.COMMUNICATION,
    ToolName.SAVE_EMAIL_DRAFT: ToolCategory.COMMUNICATION,
    ToolName.SEND_EMAIL_DRAFT: ToolCategory.COMMUNICATION,
    ToolName.READ_CHAT_ATTACHMENT: ToolCategory.COMMUNICATION,
    ToolName.LIST_CHAT_ATTACHMENTS: ToolCategory.COMMUNICATION,
    ToolName.LIST_DEAL_CHANNEL_MESSAGES: ToolCategory.COMMUNICATION,
    ToolName.FETCH_TEAMS_CHAT_MESSAGES: ToolCategory.COMMUNICATION,
    # Research tools
    ToolName.SEARCH_WIKIPEDIA: ToolCategory.SEARCH,
    ToolName.GET_STOCK_INFO: ToolCategory.SEARCH,
    ToolName.GET_COMPANY_FINANCIALS: ToolCategory.SEARCH,
    ToolName.CRAWL_WEBPAGE: ToolCategory.SEARCH,
    ToolName.SEARCH_ARXIV: ToolCategory.SEARCH,
    ToolName.EXTRACT_NEWS_ARTICLE: ToolCategory.SEARCH,
    ToolName.RESEARCH_LINKEDIN_PROFILE: ToolCategory.SEARCH,
    # Tasks
    ToolName.CREATE_TASK: ToolCategory.CRM,
    ToolName.UPDATE_TASK: ToolCategory.CRM,
    ToolName.LIST_LEADS: ToolCategory.CRM,
    ToolName.CREATE_LEAD: ToolCategory.CRM,
    ToolName.CREATE_DRAFT_LEAD_FORM: ToolCategory.CRM,
    ToolName.UPDATE_DRAFT_LEAD_FORM: ToolCategory.CRM,
    ToolName.LIST_MY_TASKS: ToolCategory.CRM,
    ToolName.LIST_DEAL_TASKS: ToolCategory.CRM,
    ToolName.CREATE_DEAL_TASK: ToolCategory.CRM,
    ToolName.COMPLETE_DEAL_TASK: ToolCategory.CRM,
    # Reminders
    ToolName.CREATE_REMINDER: ToolCategory.REMINDERS,
    ToolName.UPDATE_REMINDER: ToolCategory.REMINDERS,
    ToolName.LIST_REMINDERS: ToolCategory.REMINDERS,
    ToolName.CANCEL_REMINDER: ToolCategory.REMINDERS,
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
        # Build the factory registry once so later lookups are deterministic and
        # avoid repeated import side effects during tool resolution.
        self._register_tool_factories()

    def _register_tool_factories(self) -> None:
        """Register all available tool factories."""
        # Import here to avoid circular imports across tool modules and registry.
        from src.agents.tools.calendar import (
            create_cancel_calendar_meeting,
            create_send_calendar_invite,
            create_update_calendar_meeting,
        )
        from src.agents.tools.communication import (
            create_fetch_teams_chat_messages,
            create_get_email_details,
            create_list_chat_attachments,
            create_list_connected_email_accounts,
            create_list_deal_channel_messages,
            create_list_deal_emails,
            create_read_chat_attachment,
            create_read_email_attachment,
            create_save_email_draft,
            create_search_mailbox_emails,
            create_send_email,
            create_send_email_draft,
        )
        from src.agents.tools.crm import (
            create_add_deal_note,
            create_add_stakeholder,
            create_create_account,
            create_create_contact,
            create_create_deal,
            create_create_draft_lead_form,
            create_create_lead,
            create_create_stakeholder_if_missing,
            create_dismiss_risk_blocker,
            create_get_deal_change_history,
            create_get_deal_details,
            create_get_deal_memory,
            create_get_deal_summary,
            create_get_existing_risks_blockers,
            create_get_pipeline_details,
            create_list_deal_notes,
            create_list_deal_stakeholders,
            create_list_deals,
            create_list_leads,
            create_resolve_risk_blocker,
            create_search_accounts,
            create_search_contacts,
            create_search_deals,
            create_update_account,
            create_update_contact,
            create_update_deal_account,
            create_update_deal_fields,
            create_update_deal_lifecycle,
            create_update_deal_note,
            create_update_deal_stage,
            create_update_draft_lead_form,
            create_update_risk_blocker,
        )
        from src.agents.tools.intelligence import (
            create_get_compressed_transcript,
            create_get_meeting_deal_context,
            create_get_meeting_details,
            create_get_meeting_summary,
            create_get_meeting_transcript,
            create_list_deal_meetings,
            create_list_meetings,
            create_list_meetings_by_date,
            create_send_meeting_bot_invite,
        )
        from src.agents.tools.reminders import (
            create_cancel_reminder,
            create_create_reminder,
            create_list_reminders,
            create_update_reminder,
        )
        from src.agents.tools.research import (
            create_crawl_webpage,
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
        from src.agents.tools.tasks import (
            create_complete_deal_task,
            create_create_deal_task,
            create_create_task,
            create_list_deal_tasks,
            create_list_my_tasks,
            create_update_task,
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
        self._tool_factories[ToolName.GET_DEAL_MEMORY] = lambda: create_get_deal_memory(
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
        self._tool_factories[ToolName.LIST_LEADS] = lambda: create_list_leads(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_LEAD] = lambda: create_create_lead(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_DRAFT_LEAD_FORM] = (
            lambda: create_create_draft_lead_form(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.UPDATE_DRAFT_LEAD_FORM] = (
            lambda: create_update_draft_lead_form(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.ADD_DEAL_NOTE] = lambda: create_add_deal_note(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_DEAL_NOTE] = lambda: create_update_deal_note(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.ADD_STAKEHOLDER] = lambda: create_add_stakeholder(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_DEAL_STAGE] = lambda: create_update_deal_stage(
            self.context, self.capabilities_getter
        )

        self._tool_factories[ToolName.UPDATE_DEAL_FIELDS] = lambda: create_update_deal_fields(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_DEAL_LIFECYCLE] = lambda: create_update_deal_lifecycle(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_DEAL_ACCOUNT] = lambda: create_update_deal_account(
            self.context, self.capabilities_getter
        )  # CRM/Stakeholder tools
        self._tool_factories[ToolName.LIST_DEAL_STAKEHOLDERS] = (
            lambda: create_list_deal_stakeholders(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.CREATE_STAKEHOLDER_IF_MISSING] = (
            lambda: create_create_stakeholder_if_missing(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.GET_EXISTING_RISKS_BLOCKERS] = (
            lambda: create_get_existing_risks_blockers(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.UPDATE_RISK_BLOCKER] = lambda: create_update_risk_blocker(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.DISMISS_RISK_BLOCKER] = lambda: create_dismiss_risk_blocker(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.RESOLVE_RISK_BLOCKER] = lambda: create_resolve_risk_blocker(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_DEAL_NOTES] = lambda: create_list_deal_notes(
            self.context, self.capabilities_getter
        )
        # Contact tools
        self._tool_factories[ToolName.SEARCH_CONTACTS] = lambda: create_search_contacts(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SEARCH_ACCOUNTS] = lambda: create_search_accounts(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_CONTACT] = lambda: create_create_contact(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_ACCOUNT] = lambda: create_create_account(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_CONTACT] = lambda: create_update_contact(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_ACCOUNT] = lambda: create_update_account(
            self.context, self.capabilities_getter
        )

        # Meeting Intelligence tools
        self._tool_factories[ToolName.LIST_MEETINGS] = lambda: create_list_meetings(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SEND_MEETING_BOT_INVITE] = (
            lambda: create_send_meeting_bot_invite(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.LIST_DEAL_MEETINGS] = lambda: create_list_deal_meetings(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_MEETINGS_BY_DATE] = lambda: create_list_meetings_by_date(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SEND_CALENDAR_INVITE] = lambda: create_send_calendar_invite(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_CALENDAR_MEETING] = (
            lambda: create_update_calendar_meeting(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.CANCEL_CALENDAR_MEETING] = (
            lambda: create_cancel_calendar_meeting(self.context, self.capabilities_getter)
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
        self._tool_factories[ToolName.GET_COMPRESSED_TRANSCRIPT] = (
            lambda: create_get_compressed_transcript(self.context, self.capabilities_getter)
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
        self._tool_factories[ToolName.READ_EMAIL_ATTACHMENT] = lambda: create_read_email_attachment(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_CONNECTED_EMAIL_ACCOUNTS] = (
            lambda: create_list_connected_email_accounts(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.SEARCH_MAILBOX_EMAILS] = lambda: create_search_mailbox_emails(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SEND_EMAIL] = lambda: create_send_email(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SAVE_EMAIL_DRAFT] = lambda: create_save_email_draft(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.SEND_EMAIL_DRAFT] = lambda: create_send_email_draft(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.READ_CHAT_ATTACHMENT] = lambda: create_read_chat_attachment(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_CHAT_ATTACHMENTS] = lambda: create_list_chat_attachments(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_DEAL_CHANNEL_MESSAGES] = (
            lambda: create_list_deal_channel_messages(self.context, self.capabilities_getter)
        )
        self._tool_factories[ToolName.FETCH_TEAMS_CHAT_MESSAGES] = (
            lambda: create_fetch_teams_chat_messages(self.context, self.capabilities_getter)
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
        self._tool_factories[ToolName.RESEARCH_LINKEDIN_PROFILE] = (
            lambda: create_research_linkedin_profile(self.context, self.capabilities_getter)
        )
        # Task tools
        self._tool_factories[ToolName.CREATE_TASK] = lambda: create_create_task(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_TASK] = lambda: create_update_task(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_MY_TASKS] = lambda: create_list_my_tasks(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_DEAL_TASKS] = lambda: create_list_deal_tasks(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CREATE_DEAL_TASK] = lambda: create_create_deal_task(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.COMPLETE_DEAL_TASK] = lambda: create_complete_deal_task(
            self.context, self.capabilities_getter
        )

        # Reminder tools
        self._tool_factories[ToolName.CREATE_REMINDER] = lambda: create_create_reminder(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.UPDATE_REMINDER] = lambda: create_update_reminder(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.LIST_REMINDERS] = lambda: create_list_reminders(
            self.context, self.capabilities_getter
        )
        self._tool_factories[ToolName.CANCEL_REMINDER] = lambda: create_cancel_reminder(
            self.context, self.capabilities_getter
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
        excluded_tools = get_excluded_tools_for_context(self.context)
        tools: list[Callable] = []

        for tool_name in allowed_tools:
            # Apply source-context exclusions before channel gating; scheduled
            # alerts must never receive reminder tools even on allowed channels.
            if tool_name in excluded_tools:
                logger.debug(f"Skipping tool {tool_name} for {agent_name} due to context")
                continue

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


def get_excluded_tools_for_context(context: AgentContext) -> set[ToolName]:
    """Return tools that must be hidden for the current execution context."""
    if context.metadata.get("source") == "scheduled_alert":
        return REMINDER_MANAGEMENT_TOOLS

    if (
        context.channel == "whatsapp"
        and context.metadata.get("whatsapp_action_authority") == "none"
    ):
        return WHATSAPP_MUTATING_TOOLS

    return set()


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
    "GENERAL_AGENT_TOOLS",
    "TOOL_CATEGORIES",
    "REMINDER_MANAGEMENT_TOOLS",
    "VOICE_REALTIME_WRITE_EXCLUDED_TOOLS",
    "WHATSAPP_MUTATING_TOOLS",
    "get_excluded_tools_for_context",
    "get_tools_for_agent",
]
