from uuid import uuid4

from src.agents.context import AgentContext
from src.agents.tools.registry import VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
from src.service.voice_realtime.tool_manifest import (
    VOICE_REALTIME_SUPPORTED_TOOLS,
    build_voice_allowed_tools,
    build_voice_tool_factories,
    build_voice_tool_schemas,
)


def test_voice_allowed_tools_exclude_backend_write_tools() -> None:
    allowed_tool_names = set(build_voice_allowed_tools())

    assert allowed_tool_names.isdisjoint(
        {tool.value for tool in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS}
    )


def test_every_voice_schema_name_is_in_allowed_tools() -> None:
    allowed_tool_names = set(build_voice_allowed_tools())
    schema_names = {schema["name"] for schema in build_voice_tool_schemas()}

    assert schema_names == allowed_tool_names


def test_every_supported_voice_tool_has_an_executor_factory() -> None:
    factories = build_voice_tool_factories(AgentContext(organisation_id=uuid4()))

    assert set(factories) == set(VOICE_REALTIME_SUPPORTED_TOOLS)
