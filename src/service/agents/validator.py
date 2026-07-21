"""Validation boundary for persisted user-agent definitions.

API schemas reject malformed request shapes, while this module enforces code-owned tool and
skill catalogs plus configuration rules that must also hold for future worker or internal
callers. Stored capability names remain allowlists; runtime permission checks still apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from src.agents.tools.registry import GENERAL_AGENT_TOOLS
from src.api.schemas.agent_models import AgentDefinitionBase
from src.domain.models.organisation_tables import AgentOutputMode
from src.service.agents.capability_catalog import discover_skill_keys
from src.service.agents.errors import AgentServiceError
from src.service.automation.config_validation import (
    contains_credentials,
    validate_json_document,
)

# copilotgtm: V1 keeps small configuration documents inline with the agent row. Move large
# schemas to immutable blob-backed artifacts only if real agent contracts outgrow these ceilings.
_MODEL_CONFIG_MAX_BYTES = 16_384
_SCHEMA_MAX_BYTES = 65_536


class AgentDefinitionValidator:
    """Validate one complete definition against backend-owned capability catalogs."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or Path(__file__).resolve().parents[2] / "agents" / "skills"

    def validate_definition(self, definition: AgentDefinitionBase) -> None:
        """Reject unsafe or unrunnable configuration before it reaches persistence."""

        self.validate_capabilities(definition.allowed_tools, definition.allowed_skills)
        self._validate_json_document(
            "agent_model_config", definition.agent_model_config, max_bytes=_MODEL_CONFIG_MAX_BYTES
        )
        if contains_credentials(definition.agent_model_config):
            raise AgentServiceError("agent_model_config must not contain credentials")

        self._validate_schema("input_schema", definition.input_schema)
        self._validate_schema("output_schema", definition.output_schema)
        if definition.output_mode == AgentOutputMode.JSON and definition.output_schema is None:
            raise AgentServiceError("output_schema is required when output_mode is json")

    def validate_capabilities(
        self,
        allowed_tools: list[str],
        allowed_skills: list[str],
    ) -> None:
        """Reject capability keys that this backend cannot resolve."""

        known_tools = {tool.value for tool in GENERAL_AGENT_TOOLS}
        unknown_tools = sorted(set(allowed_tools) - known_tools)
        if unknown_tools:
            raise AgentServiceError(f"Unknown agent tools: {', '.join(unknown_tools)}")

        known_skills = set(discover_skill_keys(self.skills_dir))
        unknown_skills = sorted(set(allowed_skills) - known_skills)
        if unknown_skills:
            raise AgentServiceError(f"Unknown agent skills: {', '.join(unknown_skills)}")

    def _validate_schema(self, field_name: str, schema: dict[str, Any] | None) -> None:
        if schema is None:
            return
        self._validate_json_document(field_name, schema, max_bytes=_SCHEMA_MAX_BYTES)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AgentServiceError(
                f"{field_name} is not valid JSON Schema: {exc.message}"
            ) from exc

    @staticmethod
    def _validate_json_document(field_name: str, value: Any, *, max_bytes: int) -> None:
        try:
            validate_json_document(field_name, value, max_bytes=max_bytes)
        except ValueError as exc:
            raise AgentServiceError(str(exc)) from exc
