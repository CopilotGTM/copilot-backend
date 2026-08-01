"""Regression tests for the realtime voice tool allowlist and public schemas."""

import inspect
from uuid import uuid4

import pytest
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
    assert ToolName.SEND_EMAIL.value not in allowed_tool_names
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


def test_every_advertised_voice_tool_has_an_executor_factory() -> None:
    """Every currently advertised voice tool must have a backend callable factory."""
    factories = build_voice_tool_factories(AgentContext(organisation_id=uuid4()))

    assert {tool.value for tool in factories} == set(build_voice_allowed_tools())
    assert set(factories) <= set(VOICE_REALTIME_SUPPORTED_TOOLS)


def test_every_voice_schema_argument_is_accepted_by_its_callable() -> None:
    """Advertised arguments must reach tools instead of being silently dropped by the adapter."""
    factories = build_voice_tool_factories(AgentContext(organisation_id=uuid4()))

    for schema in build_voice_tool_schemas():
        tool_name = ToolName(schema["name"])
        callable_parameters = set(inspect.signature(factories[tool_name]()).parameters)
        schema_parameters = set(schema["parameters"]["properties"])

        assert schema_parameters <= callable_parameters, schema["name"]


def test_expanded_voice_manifest_exposes_requested_general_agent_tools() -> None:
    """New realtime sessions advertise every requested research, deal, calendar, and email tool."""
    allowed_tool_names = set(build_voice_allowed_tools())

    assert {
        ToolName.GET_DEAL_MEMORY.value,
        ToolName.LIST_DEAL_STAKEHOLDERS.value,
        ToolName.UPDATE_CALENDAR_MEETING.value,
        ToolName.CANCEL_CALENDAR_MEETING.value,
        ToolName.LIST_CONNECTED_EMAIL_ACCOUNTS.value,
        ToolName.SEARCH_MAILBOX_EMAILS.value,
        ToolName.READ_EMAIL_ATTACHMENT.value,
        ToolName.SAVE_EMAIL_DRAFT.value,
        ToolName.SEND_EMAIL_DRAFT.value,
    }.issubset(allowed_tool_names)
    assert len(allowed_tool_names) == 37
    assert ToolName.SEARCH_INTERNET.value not in allowed_tool_names
    assert ToolName.CRAWL_WEBPAGE.value not in allowed_tool_names
    assert ToolName.SEND_EMAIL.value not in allowed_tool_names


def test_public_web_tools_require_their_separate_boundary_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public providers remain unavailable until operations accepts query-provenance risk."""
    monkeypatch.setenv("VOICE_REALTIME_PUBLIC_WEB_TOOLS_ENABLED", "true")

    allowed_tool_names = set(build_voice_allowed_tools())

    assert len(allowed_tool_names) == 39
    assert ToolName.SEARCH_INTERNET.value in allowed_tool_names
    assert ToolName.CRAWL_WEBPAGE.value in allowed_tool_names
    assert ToolName.SEND_EMAIL.value not in allowed_tool_names


def test_expanded_voice_manifest_can_be_disabled_without_code_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operational kill switch restores the original manifest for newly created calls."""
    monkeypatch.setenv("VOICE_REALTIME_EXPANDED_TOOLS_ENABLED", "false")

    allowed_tool_names = set(build_voice_allowed_tools())

    assert len(allowed_tool_names) == 28
    assert ToolName.SEARCH_INTERNET.value not in allowed_tool_names
    assert ToolName.UPDATE_CALENDAR_MEETING.value not in allowed_tool_names
    assert ToolName.SAVE_EMAIL_DRAFT.value not in allowed_tool_names


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
    assert "owner_user_id" in properties


def test_voice_can_resolve_crm_users_before_owner_queries() -> None:
    """Voice uses the same stable team-member identity path as text channels."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.SEARCH_CRM_USERS.value]
    assert schema["parameters"]["required"] == ["query"]
    assert "query" in schema["parameters"]["properties"]


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


def test_direct_email_send_is_hidden_from_realtime() -> None:
    """Voice email delivery must cross the provider-draft review boundary."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    assert ToolName.SEND_EMAIL.value not in schemas


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


def test_update_calendar_meeting_schema_preserves_team_scope_and_confirmation() -> None:
    """Voice meeting edits carry deterministic team resolution and exact replay fields."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    properties = schemas[ToolName.UPDATE_CALENDAR_MEETING.value]["parameters"]["properties"]
    attendee_properties = properties["attendees"]["items"]["properties"]

    assert attendee_properties["scope"]["enum"] == ["team"]
    assert properties["attendee_action"]["enum"] == ["add", "remove", "replace"]
    assert properties["notification_scope"]["enum"] == [
        "changed_attendees_only",
        "all_attendees",
        "none",
    ]
    assert "expected_version" in properties
    assert "confirmation_token" in properties
    assert "confirmed" in properties


def test_email_draft_schemas_keep_save_and_send_contracts_distinct() -> None:
    """Voice saves generated copy first and sends only an unchanged reviewed provider draft."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    save_schema = schemas[ToolName.SAVE_EMAIL_DRAFT.value]
    send_schema = schemas[ToolName.SEND_EMAIL_DRAFT.value]

    assert save_schema["parameters"]["required"] == ["to", "subject", "body"]
    assert "reply_to_internet_message_id" in save_schema["parameters"]["properties"]
    assert send_schema["parameters"]["required"] == ["draft_id"]
    assert "expected_content_hash" in send_schema["parameters"]["properties"]
    assert "confirmed" in send_schema["parameters"]["properties"]


def test_mailbox_search_schema_exposes_thread_attention_filters() -> None:
    """Realtime mailbox triage can select thread mode and the supported attention categories."""
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    properties = schemas[ToolName.SEARCH_MAILBOX_EMAILS.value]["parameters"]["properties"]

    assert properties["result_mode"]["enum"] == ["messages", "threads"]
    assert "pending_from_me" in properties["attention_filter"]["enum"]
    assert "waiting_on_them" in properties["attention_filter"]["enum"]


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
