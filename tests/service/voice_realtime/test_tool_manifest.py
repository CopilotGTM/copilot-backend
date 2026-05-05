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
    allowed_tool_names = set(build_voice_allowed_tools())

    # Direct voice-safe write paths are allowed; remaining write tools stay excluded.
    excluded_tools = VOICE_REALTIME_WRITE_EXCLUDED_TOOLS - {
        ToolName.CREATE_DEAL,
        ToolName.CREATE_TASK,
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
    }
    assert allowed_tool_names.isdisjoint({tool.value for tool in excluded_tools})
    assert ToolName.CREATE_DEAL.value in allowed_tool_names
    assert ToolName.CREATE_TASK.value in allowed_tool_names
    assert ToolName.CREATE_REMINDER.value in allowed_tool_names
    assert ToolName.LIST_REMINDERS.value in allowed_tool_names
    assert ToolName.CANCEL_REMINDER.value in allowed_tool_names
    assert ToolName.CREATE_TASK not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.CREATE_REMINDER not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS


def test_every_voice_schema_name_is_in_allowed_tools() -> None:
    allowed_tool_names = set(build_voice_allowed_tools())
    schema_names = {schema["name"] for schema in build_voice_tool_schemas()}

    assert schema_names == allowed_tool_names


def test_every_supported_voice_tool_has_an_executor_factory() -> None:
    factories = build_voice_tool_factories(AgentContext(organisation_id=uuid4()))

    assert set(factories) == set(VOICE_REALTIME_SUPPORTED_TOOLS)


def test_create_deal_voice_schema_is_available_for_direct_write() -> None:
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_DEAL.value]
    assert schema["description"].startswith("Create a new CRM deal")
    assert schema["parameters"]["required"] == ["name"]
    assert "amount" in schema["parameters"]["properties"]


def test_create_task_voice_schema_is_available_for_direct_write() -> None:
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_TASK.value]
    assert schema["description"].startswith("Create a CRM task")
    assert schema["parameters"]["required"] == ["title"]
    assert "due_date" in schema["parameters"]["properties"]
    assert "reminder" in schema["parameters"]["properties"]


def test_create_reminder_voice_schema_is_available_for_direct_write() -> None:
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}

    schema = schemas[ToolName.CREATE_REMINDER.value]
    assert schema["description"].startswith("Create a reminder")
    assert schema["parameters"]["required"] == ["title", "trigger_time"]
    assert "message" in schema["parameters"]["properties"]
    assert "agent_prompt" in schema["parameters"]["properties"]
