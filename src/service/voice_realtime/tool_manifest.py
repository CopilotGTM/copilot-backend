from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from src.agents.tools.registry import ToolName

if TYPE_CHECKING:
    from src.agents.context import AgentContext


@dataclass(frozen=True, slots=True)
class VoiceRealtimeToolDefinition:
    tool_name: ToolName
    description: str
    parameters: dict[str, Any]
    factory_builder: Callable[[AgentContext], Callable[[], Callable[..., Any]]]

    def build_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.tool_name.value,
            "description": self.description,
            "parameters": deepcopy(self.parameters),
        }


def _build_empty_capabilities() -> None:
    return None


def _build_search_internal_knowledge_factory(
    context: AgentContext,
) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.search.internal_knowledge_base import create_search_internal_knowledge

    return partial(create_search_internal_knowledge, context, _build_empty_capabilities)


def _build_search_deals_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.search_deals import create_search_deals

    return partial(create_search_deals, context, _build_empty_capabilities)


def _build_list_deals_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.list_deals import create_list_deals

    return partial(create_list_deals, context, _build_empty_capabilities)


def _build_get_deal_details_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.get_deal_details import create_get_deal_details

    return partial(create_get_deal_details, context, _build_empty_capabilities)


def _build_get_deal_summary_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.get_deal_summary import create_get_deal_summary

    return partial(create_get_deal_summary, context, _build_empty_capabilities)


def _build_get_existing_risks_blockers_factory(
    context: AgentContext,
) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.get_risks_blockers import create_get_existing_risks_blockers

    return partial(create_get_existing_risks_blockers, context, _build_empty_capabilities)


def _build_list_deal_notes_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.list_deal_notes import create_list_deal_notes

    return partial(create_list_deal_notes, context, _build_empty_capabilities)


def _build_search_contacts_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.search_contacts import create_search_contacts

    return partial(create_search_contacts, context, _build_empty_capabilities)


def _build_search_accounts_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.crm.search_accounts import create_search_accounts

    return partial(create_search_accounts, context, _build_empty_capabilities)


def _build_list_meetings_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.list_meetings_unified import create_list_meetings

    return partial(create_list_meetings, context, _build_empty_capabilities)


def _build_send_meeting_bot_invite_factory(
    context: AgentContext,
) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.send_meeting_bot_invite import (
        create_send_meeting_bot_invite,
    )

    return partial(create_send_meeting_bot_invite, context, _build_empty_capabilities)


def _build_send_calendar_invite_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.calendar.send_calendar_invite import create_send_calendar_invite

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_send_calendar_invite, voice_context, _build_empty_capabilities)


def _build_list_deal_meetings_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.list_meetings import create_list_deal_meetings

    return partial(create_list_deal_meetings, context, _build_empty_capabilities)


def _build_list_meetings_by_date_factory(
    context: AgentContext,
) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.list_meetings_by_date import create_list_meetings_by_date

    return partial(create_list_meetings_by_date, context, _build_empty_capabilities)


def _build_get_meeting_details_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.get_meeting_details import create_get_meeting_details

    return partial(create_get_meeting_details, context, _build_empty_capabilities)


def _build_get_meeting_summary_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.get_meeting_summary import create_get_meeting_summary

    return partial(create_get_meeting_summary, context, _build_empty_capabilities)


def _build_get_meeting_transcript_factory(
    context: AgentContext,
) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.intelligence.get_meeting_transcript import create_get_meeting_transcript

    return partial(create_get_meeting_transcript, context, _build_empty_capabilities)


def _build_list_deal_emails_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.communication.list_emails import create_list_deal_emails

    return partial(create_list_deal_emails, context, _build_empty_capabilities)


def _build_get_email_details_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.communication.get_email_details import create_get_email_details

    return partial(create_get_email_details, context, _build_empty_capabilities)


def _build_send_email_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.communication.send_email import create_send_email

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_send_email, voice_context, _build_empty_capabilities)


def _build_add_deal_note_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.crm.add_deal_note import create_add_deal_note

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_add_deal_note, voice_context, _build_empty_capabilities)


def _build_add_stakeholder_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.crm.add_stakeholder import create_add_stakeholder

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_add_stakeholder, voice_context, _build_empty_capabilities)


def _build_create_deal_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.crm.create_deal import create_create_deal

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_create_deal, voice_context, _build_empty_capabilities)


def _build_create_contact_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.crm.create_contact import create_create_contact

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_create_contact, voice_context, _build_empty_capabilities)


def _build_create_account_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.crm.create_account import create_create_account

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_create_account, voice_context, _build_empty_capabilities)


def _build_create_task_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.tasks.create_task import create_create_task

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_create_task, voice_context, _build_empty_capabilities)


def _build_create_reminder_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.reminders.create_reminder import create_create_reminder

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_create_reminder, voice_context, _build_empty_capabilities)


def _build_list_reminders_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.reminders.list_reminders import create_list_reminders

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_list_reminders, voice_context, _build_empty_capabilities)


def _build_cancel_reminder_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.context import AgentContext
    from src.agents.tools.reminders.cancel_reminder import create_cancel_reminder

    voice_context = AgentContext(
        organisation_id=context.organisation_id,
        deal_id=context.deal_id,
        org_name=context.org_name,
        user_id=context.user_id,
        user_email=context.user_email,
        channel="voice",
        message_id=context.message_id,
        metadata=context.metadata.copy(),
    )
    return partial(create_cancel_reminder, voice_context, _build_empty_capabilities)


VOICE_REALTIME_TOOL_DEFINITIONS: tuple[VoiceRealtimeToolDefinition, ...] = (
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEARCH_INTERNAL_KNOWLEDGE,
        description="Search internal company knowledge and documents.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        factory_builder=_build_search_internal_knowledge_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEARCH_DEALS,
        description="Find deals by name or company before drilling into a specific deal.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        factory_builder=_build_search_deals_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_DEALS,
        description="List deals with filters such as status, amount, owner, or company.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "status": {"type": "string"},
                "stage_name": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "owner_email": {"type": "string"},
                "company_name": {"type": "string"},
                "sort_by": {"type": "string"},
                "sort_order": {"type": "string", "enum": ["asc", "desc"]},
                "limit": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_deals_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CREATE_DEAL,
        description=(
            "Create a new CRM deal directly once the user clearly provides the deal name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string"},
                "description": {"type": "string"},
                "close_date": {"type": "string"},
                "priority": {"type": "string"},
                "account_name": {"type": "string"},
                "account_domain": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        factory_builder=_build_create_deal_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CREATE_ACCOUNT,
        description="Create or reuse a CRM account once the user provides a name, domain, or website.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "domain": {"type": "string"},
                "website": {"type": "string"},
                "industry": {"type": "string"},
                "linkedin_url": {"type": "string"},
                "phone": {"type": "string"},
                "country": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        factory_builder=_build_create_account_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CREATE_CONTACT,
        description="Create or reuse a CRM contact once the user provides name and email.",
        parameters={
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "name": {"type": "string"},
                "job_title": {"type": "string"},
                "phone": {"type": "string"},
                "linkedin_url": {"type": "string"},
                "account_id": {"type": "string"},
                "account_name": {"type": "string"},
                "account_domain": {"type": "string"},
                "deal_id": {"type": "string"},
                "deal_name": {"type": "string"},
                "relationship_role": {"type": "string"},
            },
            "required": ["email", "name"],
            "additionalProperties": False,
        },
        factory_builder=_build_create_contact_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CREATE_TASK,
        description=(
            "Create a CRM task directly once the user clearly provides a meaningful task title."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"]},
                "due_date": {
                    "type": "string",
                    "description": "Raw local due date/time expression. Do not convert to UTC.",
                },
                "deal_name": {"type": "string"},
                "deal_id": {"type": "string"},
                "assign_to": {"type": "string"},
                "reminder": {
                    "type": "string",
                    "enum": ["at_due", "15m_before", "1h_before", "1d_before"],
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        factory_builder=_build_create_task_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.ADD_DEAL_NOTE,
        description=(
            "Create a note directly on an existing deal once the deal and note content are clear."
        ),
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "deal_name": {"type": "string"},
                "body": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["deal_id", "deal_name", "body"],
            "additionalProperties": False,
        },
        factory_builder=_build_add_deal_note_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.ADD_STAKEHOLDER,
        description=(
            "Create an external stakeholder directly on an existing deal once the email is known."
        ),
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "deal_name": {"type": "string"},
                "email": {"type": "string"},
                "name": {"type": "string"},
                "job_title": {"type": "string"},
                "phone_number": {"type": "string"},
                "department": {"type": "string"},
                "stance": {
                    "type": "string",
                    "enum": [
                        "UNKNOWN",
                        "CHAMPION",
                        "SUPPORTER",
                        "NEUTRAL",
                        "DETRACTOR",
                        "BLOCKER",
                    ],
                },
                "decision_power": {
                    "type": "string",
                    "enum": [
                        "UNKNOWN",
                        "DECISION_MAKER",
                        "INFLUENCER",
                        "EVALUATOR",
                        "GATEKEEPER",
                        "END_USER",
                    ],
                },
                "motivation": {"type": "string"},
                "pain_points": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["deal_id", "deal_name", "email"],
            "additionalProperties": False,
        },
        factory_builder=_build_add_stakeholder_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CREATE_REMINDER,
        description=(
            "Create a reminder directly once the user provides a meaningful title and trigger "
            "time. Pass the user's trigger time exactly as spoken or typed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "trigger_time": {
                    "type": "string",
                    "description": (
                        "Raw local time expression exactly as spoken or typed, such as "
                        "'10 AM', '12 AM', 'Saturday 10 PM', or 'tomorrow at 9'. "
                        "Do not convert to UTC."
                    ),
                },
                "reminder_type": {"type": "string", "enum": ["static", "agentic"]},
                "message": {"type": "string"},
                "agent_prompt": {"type": "string"},
                "deal_id": {"type": "string"},
                "recurrence": {
                    "type": "string",
                    "enum": ["none", "daily", "weekdays", "weekly", "biweekly", "monthly"],
                },
            },
            "required": ["title", "trigger_time"],
            "additionalProperties": False,
        },
        factory_builder=_build_create_reminder_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_REMINDERS,
        description="List the user's active reminders and scheduled alerts.",
        parameters={
            "type": "object",
            "properties": {
                "include_completed": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_reminders_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.CANCEL_REMINDER,
        description="Cancel one of the user's reminders by reminder ID.",
        parameters={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_cancel_reminder_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_DEAL_DETAILS,
        description="Get full details for a specific deal.",
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "include_stakeholders": {"type": "boolean"},
                "include_risks": {"type": "boolean"},
            },
            "required": ["deal_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_deal_details_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_DEAL_SUMMARY,
        description="Get the AI-generated summary for a specific deal.",
        parameters={
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_deal_summary_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_EXISTING_RISKS_BLOCKERS,
        description="List existing risks and blockers for a deal.",
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "include_dismissed": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_get_existing_risks_blockers_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_DEAL_NOTES,
        description="List CRM notes associated with a deal.",
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "limit": {"type": "integer"},
                "include_body": {"type": "boolean"},
                "max_body_length": {"type": "integer"},
                "include_properties": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_deal_notes_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEARCH_CONTACTS,
        description="Search contacts by name, email, or company.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "company_name": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_search_contacts_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEARCH_ACCOUNTS,
        description="Search CRM accounts by name or domain.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        factory_builder=_build_search_accounts_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_MEETINGS,
        description="List meetings from the current user's connected calendar.",
        parameters={
            "type": "object",
            "properties": {
                "start_datetime": {"type": "string"},
                "end_datetime": {"type": "string"},
                "limit": {"type": "integer"},
                "deal_id": {"type": "string"},
                "attendee_email": {"type": "string"},
                "attendee_name": {"type": "string"},
                "has_transcript": {"type": "boolean"},
                "has_summary": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_meetings_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEND_MEETING_BOT_INVITE,
        description="Send a recording bot to a user-owned calendar meeting.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_send_meeting_bot_invite_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEND_CALENDAR_INVITE,
        description=(
            "Create a calendar meeting invite with a meeting link from the user's connected "
            "calendar account."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {
                    "type": "string",
                    "description": "Raw local start time expression. Do not convert to UTC.",
                },
                "end_time": {
                    "type": "string",
                    "description": "Raw local end time expression. Do not convert to UTC.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Attendee emails or raw attendee names exactly as provided. Do not ask "
                        "for email first when the user gives a name."
                    ),
                },
                "body": {"type": "string"},
                "location": {"type": "string"},
                "deal_id": {"type": "string"},
                "timezone": {
                    "type": "string",
                    "description": (
                        "Only pass when the user explicitly names a timezone. Otherwise omit; "
                        "the backend uses the captured app timezone."
                    ),
                },
                "sender_email": {
                    "type": "string",
                    "description": (
                        "Calendar sender mailbox. If the invite tool returns "
                        "available_sender_accounts, ask together with any missing title, time, "
                        "or attendee fields."
                    ),
                },
                "confirmed": {"type": "boolean"},
            },
            "required": ["title", "start_time", "end_time", "attendees"],
            "additionalProperties": False,
        },
        factory_builder=_build_send_calendar_invite_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_MEETING_DETAILS,
        description="Get detailed information for a specific meeting.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "include_transcript": {"type": "boolean"},
                "include_summary": {"type": "boolean"},
                "include_prep": {"type": "boolean"},
                "include_followup": {"type": "boolean"},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_meeting_details_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_MEETING_SUMMARY,
        description="Get the AI-generated summary for a meeting.",
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_meeting_summary_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_MEETING_TRANSCRIPT,
        description="Get the transcript for a meeting.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "output_format": {"type": "string"},
                "max_length": {"type": "integer"},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_meeting_transcript_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_DEAL_EMAILS,
        description="List emails associated with a deal.",
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "limit": {"type": "integer"},
                "direction": {"type": "string"},
                "status": {"type": "string"},
                "sender_email": {"type": "string"},
                "sender_name": {"type": "string"},
                "include_body_preview": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_deal_emails_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.GET_EMAIL_DETAILS,
        description="Get the full details for an email message.",
        parameters={
            "type": "object",
            "properties": {
                "internet_message_id": {"type": "string"},
                "include_body": {"type": "boolean"},
                "include_html": {"type": "boolean"},
                "max_body_length": {"type": "integer"},
            },
            "required": ["internet_message_id"],
            "additionalProperties": False,
        },
        factory_builder=_build_get_email_details_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.SEND_EMAIL,
        description=(
            "Send a new Gmail or Outlook email when explicit recipient emails, subject, and body "
            "are known."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
                "bcc": {"type": "array", "items": {"type": "string"}},
                "deal_id": {"type": "string"},
                "sender_email": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
        factory_builder=_build_send_email_factory,
    ),
)

VOICE_REALTIME_SUPPORTED_TOOLS: tuple[ToolName, ...] = tuple(
    definition.tool_name for definition in VOICE_REALTIME_TOOL_DEFINITIONS
)


def build_voice_allowed_tools() -> list[str]:
    return [definition.tool_name.value for definition in VOICE_REALTIME_TOOL_DEFINITIONS]


def build_voice_tool_schemas() -> list[dict[str, Any]]:
    return [definition.build_schema() for definition in VOICE_REALTIME_TOOL_DEFINITIONS]


def build_voice_tool_factories(
    context: AgentContext,
) -> dict[ToolName, Callable[[], Callable[..., Any]]]:
    return {
        definition.tool_name: definition.factory_builder(context)
        for definition in VOICE_REALTIME_TOOL_DEFINITIONS
    }


__all__ = [
    "VOICE_REALTIME_SUPPORTED_TOOLS",
    "VOICE_REALTIME_TOOL_DEFINITIONS",
    "VoiceRealtimeToolDefinition",
    "build_voice_allowed_tools",
    "build_voice_tool_factories",
    "build_voice_tool_schemas",
]
