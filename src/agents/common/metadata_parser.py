from __future__ import annotations

import json
from collections.abc import Iterator

from src.agents.common.json_extractor import JsonExtractor
from src.api.schemas.chat_stream_models import (
    AgentSwitch,
    AnswerMetrics,
    FieldSingleSelect,
    QuestionPrompt,
    SourcesReplace,
    SuggestionsReplace,
)
from src.infra.utils.logger import get_logger

logger = get_logger()


class MetadataParser:
    """Parse metadata JSON into frame objects."""

    def __init__(self, json_extractor: JsonExtractor):
        self.json_extractor = json_extractor

    def parse(self, buffer: str) -> Iterator:
        json_text = self.json_extractor.extract(buffer)
        if not json_text:
            logger.error(f"Failed to locate JSON metadata in buffer: {buffer}")
            return

        try:
            response_data = json.loads(json_text)
            logger.info(f"Parsed JSON metadata: {response_data}")
            self._validate_metadata_version(response_data)

            yield from self._yield_confidence_frame(response_data)
            yield from self._yield_suggestions_frame(response_data)
            yield from self._yield_sources_frame(response_data)
            yield from self._yield_question_frame(response_data)
            yield from self._yield_agent_switch_frame(response_data)

        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON metadata: {json_text}")

    def _validate_metadata_version(self, response_data: dict):
        if not isinstance(response_data, dict):
            return

        version = str(response_data.get("version", "")).lower()
        valid_versions = {"aep/1", "1", "v1", "aep:v1"}

        if version and version not in valid_versions:
            logger.warning(f"Unrecognized metadata version: {version}")

    def _yield_confidence_frame(self, response_data: dict):
        if "confidence_score" in response_data:
            confidence = self._convert_confidence_score(response_data.get("confidence_score"))
            yield AnswerMetrics(confidence=confidence)

    def _yield_suggestions_frame(self, response_data: dict):
        if "suggested_questions" in response_data:
            suggestions = [str(q) for q in response_data.get("suggested_questions", [])]
            if suggestions:
                yield SuggestionsReplace(items=suggestions)

    def _yield_sources_frame(self, response_data: dict):
        sources = response_data.get("sources") or response_data.get("citations")
        if sources:
            yield SourcesReplace(sources=sources)

    def _yield_question_frame(self, response_data: dict):
        ask = response_data.get("ask")
        if not isinstance(ask, dict):
            return

        field_specs = ask.get("fields", [])
        if field_specs:
            main_field = None
            for spec in field_specs:
                if spec.get("options"):
                    main_field = self._create_single_select_field(spec)
                    break

            if not main_field and field_specs:
                main_field = self._create_single_select_field(field_specs[0])

            if main_field:
                yield QuestionPrompt(
                    prompt=ask.get("prompt") or "",
                    fields=[main_field],
                    await_user_response=bool(ask.get("await_user_response", True)),
                )

    def _create_single_select_field(self, spec: dict) -> FieldSingleSelect | None:
        name = spec.get("name") or "input"
        required = bool(spec.get("required", False))

        options = spec.get("options", [])
        if not options:
            options = ["Option 1", "Option 2", "Option 3"]

        return FieldSingleSelect(
            name=name,
            options=[str(option) for option in options],
            required=required,
        )

    def _yield_agent_switch_frame(self, response_data: dict):
        switch = (
            response_data.get("switch_agent")
            or response_data.get("agent_switch")
            or response_data.get("switch")
        )

        logger.info(
            f"Checking for agent switch in response_data keys: {list(response_data.keys())}"
        )
        logger.info(f"Switch data found: {switch}")

        if isinstance(switch, dict) and switch.get("to"):
            logger.info(f"Agent switch detected - switching to: {switch.get('to')}")
            yield AgentSwitch(
                from_agent="GENERAL_CHAT",
                to_agent=switch.get("to"),
                reason=switch.get("reason") or "Agent switch requested",
            )
        elif isinstance(switch, str) and switch.strip():
            logger.info(f"Agent switch detected (string format) - switching to: {switch}")
            yield AgentSwitch(
                from_agent="GENERAL_CHAT",
                to_agent=switch.strip(),
                reason="Agent switch requested",
            )
        else:
            if any(key.lower().startswith(("switch", "agent")) for key in response_data):
                logger.info(
                    "Potential agent switch fields found but not in expected format: "
                    f"{[k for k in response_data if 'switch' in k.lower() or 'agent' in k.lower()]}"
                )
            else:
                logger.info("No agent switch detected in response data")

    def _convert_confidence_score(self, confidence_value) -> str | None:
        if confidence_value is None:
            return None

        if isinstance(confidence_value, str) and confidence_value in [
            "high",
            "medium",
            "low",
            "neutral",
        ]:
            return confidence_value

        if isinstance(confidence_value, int | float):
            if confidence_value >= 0.8:
                return "high"
            if confidence_value >= 0.5:
                return "medium"
            return "low"

        return "medium"
