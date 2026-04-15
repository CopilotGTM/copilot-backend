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
        tool_name=ToolName.LIST_DEAL_MEETINGS,
        description="List meetings associated with a deal.",
        parameters={
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "limit": {"type": "integer"},
                "include_past": {"type": "boolean"},
                "include_upcoming": {"type": "boolean"},
                "attendee_email": {"type": "string"},
                "attendee_name": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_deal_meetings_factory,
    ),
    VoiceRealtimeToolDefinition(
        tool_name=ToolName.LIST_MEETINGS_BY_DATE,
        description="List meetings by date range or relative window.",
        parameters={
            "type": "object",
            "properties": {
                "filter_type": {"type": "string"},
                "days": {"type": "integer"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "deal_id": {"type": "string"},
                "has_transcript": {"type": "boolean"},
                "has_summary": {"type": "boolean"},
                "attendee_email": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        factory_builder=_build_list_meetings_by_date_factory,
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
