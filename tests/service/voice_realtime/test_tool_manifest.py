"""Regression tests for the realtime voice tool allowlist and public schemas."""

from uuid import uuid4

from src.agents.context import AgentContext
from src.agents.tools.registry import VOICE_REALTIME_WRITE_EXCLUDED_TOOLS, ToolName
from src.service.voice_realtime.tool_manifest import (
    VOICE_REALTIME_SUPPORTED_TOOLS,
    build_voice_allowed_tools,
    build_voice_tool_factories,
    build_voice_tool_schemas,
)


def test_voice_allowed_tools_exclude_backend_write_tools() -> None:
    """Voice may expose reviewed direct writes without leaking unsupported mutations."""
    allowed_tool_names = set(build_voice_allowed_tools())

    # Direct voice-safe write paths are allowed; remaining write tools stay excluded.
    excluded_tools = VOICE_REALTIME_WRITE_EXCLUDED_TOOLS - {
        ToolName.CREATE_DEAL,
        ToolName.ADD_DEAL_NOTE,
        ToolName.ADD_STAKEHOLDER,
        ToolName.CREATE_TASK,
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
        ToolName.SEND_CALENDAR_INVITE,
        ToolName.SEND_EMAIL,
    }
    assert allowed_tool_names.isdisjoint({tool.value for tool in excluded_tools})
    assert ToolName.CREATE_DEAL.value in allowed_tool_names
    assert ToolName.ADD_DEAL_NOTE.value in allowed_tool_names
    assert ToolName.ADD_STAKEHOLDER.value in allowed_tool_names
    assert ToolName.CREATE_TASK.value in allowed_tool_names
    assert ToolName.CREATE_REMINDER.value in allowed_tool_names
    assert ToolName.LIST_REMINDERS.value in allowed_tool_names
    assert ToolName.CANCEL_REMINDER.value in allowed_tool_names
    assert ToolName.SEND_CALENDAR_INVITE.value in allowed_tool_names
    assert ToolName.SEND_EMAIL.value in allowed_tool_names
    assert ToolName.CREATE_TASK not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.ADD_DEAL_NOTE not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.ADD_STAKEHOLDER not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.CREATE_REMINDER not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.SEND_CALENDAR_INVITE not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.SEND_EMAIL not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS


def test_every_voice_schema_name_is_in_allowed_tools() -> None:
    """The provider schema and backend allowlist must describe the same tools."""
    allowed_tool_names = set(build_voice_allowed_tools())
    schema_names = {schema["name"] for schema in build_voice_tool_schemas()}

    assert schema_names == allowed_tool_names


def test_every_supported_voice_tool_has_an_executor_factory() -> None:
    """Every advertised voice tool must have a backend callable factory."""
    factories = build_voice_tool_factories(AgentContext(organisation_id=uuid4()))

    assert set(factories) == set(VOICE_REALTIME_SUPPORTED_TOOLS)


def test_create_deal_voice_schema_is_available_for_direct_write() -> None:
    """Voice deal creation keeps the minimal direct-write contract."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_DEAL.value]
    assert schema["description"].startswith("Create a new CRM deal")
    assert schema["parameters"]["required"] == ["name"]
    assert "amount" in schema["parameters"]["properties"]
    assert "expected_close_date" in schema["parameters"]["properties"]
    assert schema["parameters"]["properties"]["custom_columns"] == {
        "type": "object",
        "description": "Custom field values keyed by deal schema field key.",
        "additionalProperties": True,
    }
    assert (
        "Expected close date"
        in schema["parameters"]["properties"]["expected_close_date"]["description"]
    )


def test_create_account_voice_schema_accepts_url_only_identity() -> None:
    """Voice account creation may start from a domain or LinkedIn identity."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_ACCOUNT.value]
    assert schema["description"].startswith("Create or reuse a CRM account")
    assert schema["parameters"]["required"] == []
    assert "domain" in schema["parameters"]["properties"]
    assert "website" in schema["parameters"]["properties"]
    assert "linkedin_url" in schema["parameters"]["properties"]
    assert "phone" not in schema["parameters"]["properties"]
    assert "country" not in schema["parameters"]["properties"]


def test_create_contact_voice_schema_accepts_name_email_or_linkedin() -> None:
    """Voice contact creation accepts any supported person identity."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_CONTACT.value]
    assert schema["description"].startswith("Create or reuse a CRM contact")
    assert schema["parameters"]["required"] == []
    assert "email" in schema["parameters"]["properties"]
    assert "name" in schema["parameters"]["properties"]
    assert "linkedin_url" in schema["parameters"]["properties"]
    assert "account_id" not in schema["parameters"]["properties"]
    assert "phone" not in schema["parameters"]["properties"]


def test_list_deals_voice_schema_supports_expected_close_date_sort() -> None:
    """Voice deal listing exposes both actual and expected close-date filters."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.LIST_DEALS.value]
    properties = schema["parameters"]["properties"]
    sort_by = schema["parameters"]["properties"]["sort_by"]
    assert "expected_close_date" in sort_by["enum"]
    assert "close_date" in sort_by["enum"]
    assert "min_close_date" in properties
    assert "max_close_date" in properties
    assert "min_expected_close_date" in properties
    assert "max_expected_close_date" in properties


def test_create_task_voice_schema_is_available_for_direct_write() -> None:
    """Voice task creation keeps its required title and optional scheduling fields."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_TASK.value]
    assert schema["description"].startswith("Create a CRM task")
    assert schema["parameters"]["required"] == ["title"]
    assert "due_date" in schema["parameters"]["properties"]
    assert "reminder" in schema["parameters"]["properties"]


def test_add_deal_note_voice_schema_is_available_for_direct_write() -> None:
    """Voice deal notes require a resolved deal and note body."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.ADD_DEAL_NOTE.value]
    assert schema["description"].startswith("Create a note directly")
    assert schema["parameters"]["required"] == ["deal_id", "deal_name", "body"]
    assert "title" in schema["parameters"]["properties"]
    assert "note_content" not in schema["parameters"]["properties"]
    assert "subject" not in schema["parameters"]["properties"]


def test_add_stakeholder_voice_schema_is_available_for_direct_write() -> None:
    """Voice stakeholder creation exposes the supported identity and role fields."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.ADD_STAKEHOLDER.value]
    assert schema["description"].startswith("Create an external stakeholder")
    assert schema["parameters"]["required"] == ["deal_id", "deal_name"]
    assert "linkedin_url" in schema["parameters"]["properties"]
    assert "job_title" in schema["parameters"]["properties"]
    assert "UNKNOWN" in schema["parameters"]["properties"]["stance"]["enum"]


def test_create_reminder_voice_schema_is_available_for_direct_write() -> None:
    """Voice reminders require a title and user-local trigger expression."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_REMINDER.value]
    assert schema["description"].startswith("Create a reminder")
    assert schema["parameters"]["required"] == ["title", "trigger_time"]
    assert "message" in schema["parameters"]["properties"]
    assert "agent_prompt" in schema["parameters"]["properties"]


def test_create_reminder_voice_schema_requires_raw_local_trigger_expression() -> None:
    """Voice must preserve spoken reminder times for backend timezone parsing."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    trigger_time_schema = schemas[ToolName.CREATE_REMINDER.value]["parameters"]["properties"][
        "trigger_time"
    ]

    assert "exactly as spoken or typed" in trigger_time_schema["description"]
    assert "10 AM" in trigger_time_schema["description"]
    assert "12 AM" in trigger_time_schema["description"]
    assert "tomorrow at 9" in trigger_time_schema["description"]


def test_send_email_voice_schema_is_available_for_direct_write() -> None:
    """Voice email sending requires the exact recipient, subject, and body."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.SEND_EMAIL.value]
    assert schema["description"].startswith("Send a new Gmail or Outlook email")
    assert schema["parameters"]["required"] == ["to", "subject", "body"]
    assert "sender_email" in schema["parameters"]["properties"]
    assert "confirmed" in schema["parameters"]["properties"]


def test_send_calendar_invite_voice_schema_is_available_for_direct_write() -> None:
    """Voice calendar creation exposes all required invite payload fields."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.SEND_CALENDAR_INVITE.value]
    assert schema["description"].startswith("Create a calendar meeting invite")
    assert schema["parameters"]["required"] == ["title", "start_time", "end_time", "attendees"]
    assert "sender_email" in schema["parameters"]["properties"]
    assert "confirmed" in schema["parameters"]["properties"]


def test_send_calendar_invite_voice_schema_discourages_timezone_override() -> None:
    """Captured app timezone remains authoritative unless the user names another."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.SEND_CALENDAR_INVITE.value]
    timezone_schema = schema["parameters"]["properties"]["timezone"]

    assert "Only pass when the user explicitly names a timezone" in (timezone_schema["description"])
    assert "captured app timezone" in timezone_schema["description"]


def test_send_calendar_invite_voice_schema_guides_bundled_clarifications() -> None:
    """Voice invite resolution should ask for all missing fields together."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.SEND_CALENDAR_INVITE.value]
    sender_schema = schema["parameters"]["properties"]["sender_email"]
    attendees_schema = schema["parameters"]["properties"]["attendees"]

    assert "raw attendee names" in attendees_schema["description"]
    assert "ask together" in sender_schema["description"]
    assert "available_sender_accounts" in sender_schema["description"]


def test_list_meetings_voice_schema_combines_identity_and_time_clues() -> None:
    """Realtime voice must expose the existing meeting search and direction arguments."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.LIST_MEETINGS.value]
    properties = schema["parameters"]["properties"]

    assert "query" in properties
    assert "full spoken request" in properties["query"]["description"]
    assert properties["query"]["minLength"] == 1
    assert properties["query"]["maxLength"] == 200
    assert properties["time_direction"]["enum"] == ["past", "present", "future"]
    assert "Monday" in properties["start_datetime"]["description"]


def test_voice_exposes_existing_read_only_profile_research() -> None:
    """Voice research accepts exact attendee email without requiring a CRM record."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.RESEARCH_LINKEDIN_PROFILE.value]

    properties = schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == []
    assert "exact invitation email" in schema["description"]
    assert properties["email"]["maxLength"] == 320
    assert properties["company"]["maxLength"] == 200
