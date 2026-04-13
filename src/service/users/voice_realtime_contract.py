from __future__ import annotations

from typing import Any

from src.agents.tools.registry import ToolName

VOICE_REALTIME_QUEUE = "voice_realtime"
VOICE_REALTIME_TASK_NAME = "chat.voice_realtime_sideband"

VOICE_REALTIME_SUPPORTED_TOOLS: tuple[ToolName, ...] = (
    ToolName.SEARCH_INTERNAL_KNOWLEDGE,
    ToolName.SEARCH_DEALS,
    ToolName.LIST_DEALS,
    ToolName.GET_DEAL_DETAILS,
    ToolName.GET_DEAL_SUMMARY,
    ToolName.GET_EXISTING_RISKS_BLOCKERS,
    ToolName.LIST_DEAL_NOTES,
    ToolName.SEARCH_CONTACTS,
    ToolName.LIST_DEAL_MEETINGS,
    ToolName.LIST_MEETINGS_BY_DATE,
    ToolName.GET_MEETING_DETAILS,
    ToolName.GET_MEETING_SUMMARY,
    ToolName.GET_MEETING_TRANSCRIPT,
    ToolName.LIST_DEAL_EMAILS,
    ToolName.GET_EMAIL_DETAILS,
)

_VOICE_REALTIME_TOOL_SCHEMAS: dict[ToolName, dict[str, Any]] = {
    ToolName.SEARCH_INTERNAL_KNOWLEDGE: {
        "type": "function",
        "name": ToolName.SEARCH_INTERNAL_KNOWLEDGE.value,
        "description": "Search internal company knowledge and documents.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    ToolName.SEARCH_DEALS: {
        "type": "function",
        "name": ToolName.SEARCH_DEALS.value,
        "description": "Find deals by name or company before drilling into a specific deal.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    ToolName.LIST_DEALS: {
        "type": "function",
        "name": ToolName.LIST_DEALS.value,
        "description": "List deals with filters such as status, amount, owner, or company.",
        "parameters": {
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
    },
    ToolName.GET_DEAL_DETAILS: {
        "type": "function",
        "name": ToolName.GET_DEAL_DETAILS.value,
        "description": "Get full details for a specific deal.",
        "parameters": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "include_stakeholders": {"type": "boolean"},
                "include_risks": {"type": "boolean"},
            },
            "required": ["deal_id"],
            "additionalProperties": False,
        },
    },
    ToolName.GET_DEAL_SUMMARY: {
        "type": "function",
        "name": ToolName.GET_DEAL_SUMMARY.value,
        "description": "Get the AI-generated summary for a specific deal.",
        "parameters": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
            "additionalProperties": False,
        },
    },
    ToolName.GET_EXISTING_RISKS_BLOCKERS: {
        "type": "function",
        "name": ToolName.GET_EXISTING_RISKS_BLOCKERS.value,
        "description": "List existing risks and blockers for a deal.",
        "parameters": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "include_dismissed": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    ToolName.LIST_DEAL_NOTES: {
        "type": "function",
        "name": ToolName.LIST_DEAL_NOTES.value,
        "description": "List CRM notes associated with a deal.",
        "parameters": {
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
    },
    ToolName.SEARCH_CONTACTS: {
        "type": "function",
        "name": ToolName.SEARCH_CONTACTS.value,
        "description": "Search contacts by name, email, or company.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "company_name": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    ToolName.LIST_DEAL_MEETINGS: {
        "type": "function",
        "name": ToolName.LIST_DEAL_MEETINGS.value,
        "description": "List meetings associated with a deal.",
        "parameters": {
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
    },
    ToolName.LIST_MEETINGS_BY_DATE: {
        "type": "function",
        "name": ToolName.LIST_MEETINGS_BY_DATE.value,
        "description": "List meetings by date range or relative window.",
        "parameters": {
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
    },
    ToolName.GET_MEETING_DETAILS: {
        "type": "function",
        "name": ToolName.GET_MEETING_DETAILS.value,
        "description": "Get detailed information for a specific meeting.",
        "parameters": {
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
    },
    ToolName.GET_MEETING_SUMMARY: {
        "type": "function",
        "name": ToolName.GET_MEETING_SUMMARY.value,
        "description": "Get the AI-generated summary for a meeting.",
        "parameters": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
    ToolName.GET_MEETING_TRANSCRIPT: {
        "type": "function",
        "name": ToolName.GET_MEETING_TRANSCRIPT.value,
        "description": "Get the transcript for a meeting.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "output_format": {"type": "string"},
                "max_length": {"type": "integer"},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
    ToolName.LIST_DEAL_EMAILS: {
        "type": "function",
        "name": ToolName.LIST_DEAL_EMAILS.value,
        "description": "List emails associated with a deal.",
        "parameters": {
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
    },
    ToolName.GET_EMAIL_DETAILS: {
        "type": "function",
        "name": ToolName.GET_EMAIL_DETAILS.value,
        "description": "Get the full details for an email message.",
        "parameters": {
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
    },
}


def build_voice_realtime_allowed_tools() -> list[str]:
    return [tool_name.value for tool_name in VOICE_REALTIME_SUPPORTED_TOOLS]


def build_voice_realtime_tool_schemas() -> list[dict[str, Any]]:
    return [
        dict(_VOICE_REALTIME_TOOL_SCHEMAS[tool_name])
        for tool_name in VOICE_REALTIME_SUPPORTED_TOOLS
    ]


__all__ = [
    "VOICE_REALTIME_QUEUE",
    "VOICE_REALTIME_SUPPORTED_TOOLS",
    "VOICE_REALTIME_TASK_NAME",
    "build_voice_realtime_allowed_tools",
    "build_voice_realtime_tool_schemas",
]
