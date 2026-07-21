"""Execute one pinned agent run with selected backend tools and skills.

The Celery worker supplies a claimed runtime envelope and immutable
`AgentContext`. This module performs the slow model/tool work without owning a
database session; tools acquire short tenant sessions through the context.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel
from src.agents.common.token_tracking import extract_token_usage
from src.agents.context import AgentContext
from src.agents.workflows.user_configured.agent import UserConfiguredAgent
from src.service.automation.triggers.runtime import AgentRunRuntimeEnvelope


@dataclass(frozen=True)
class AgentExecutionResult:
    """Normalized model output and usage metadata persisted on `AgentRun`."""

    output_text: str | None
    output_json: Any | None
    model_name: str | None
    token_usage: dict[str, Any] | None
    cost: Decimal | None
    latency_ms: int


class AgentRunExecutor:
    """Execute a dedicated user-configured agent from a pinned run snapshot."""

    def __init__(
        self,
        *,
        agent_factory: Callable[..., Any] = UserConfiguredAgent,
    ) -> None:
        self.agent_factory = agent_factory

    async def execute(
        self,
        envelope: AgentRunRuntimeEnvelope,
        *,
        context: AgentContext,
    ) -> AgentExecutionResult:
        """Run the pinned configuration and validate any structured output."""

        self._validate_context(envelope, context)
        agent = self.agent_factory(
            context=context,
            prompt=envelope.prompt,
            model_name=envelope.model_name,
            model_settings=envelope.model_config,
            allowed_tools=envelope.allowed_tools,
            allowed_skills=envelope.allowed_skills,
            max_turns=envelope.max_turns,
            output_mode=envelope.output_mode,
            output_schema=envelope.output_schema,
        )

        # The event snapshot is the only user message. Mutable agent or trigger
        # rows are intentionally not consulted after the claim commits.
        message = json.dumps(
            {
                "goal": envelope.task_goal,
                "event": envelope.input_snapshot,
            },
            default=str,
            sort_keys=True,
        )
        started = perf_counter()
        response = await agent.run(message)
        elapsed_ms = max(0, int((perf_counter() - started) * 1000))

        output_text, output_json = self._normalize_output(envelope, response.content)
        usage = extract_token_usage(response, agent_name="AgentRun")
        usage_dict = usage.to_dict() if usage else None
        effective_model = (
            usage.model_name
            if usage and usage.model_name
            else str(getattr(response, "model", "") or agent.model_name)
        )
        cost = Decimal(str(usage.cost)) if usage and usage.cost is not None else None
        latency_ms = (
            max(0, int(usage.duration_seconds * 1000))
            if usage and usage.duration_seconds is not None
            else elapsed_ms
        )
        return AgentExecutionResult(
            output_text=output_text,
            output_json=output_json,
            model_name=effective_model,
            token_usage=usage_dict,
            cost=cost,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _validate_context(
        envelope: AgentRunRuntimeEnvelope,
        context: AgentContext,
    ) -> None:
        # These comparisons protect the immutable tenant, user, and deal
        # boundaries before any selected tool receives the context.
        if context.organisation_id != envelope.organisation_id:
            raise ValueError("context organisation does not match agent run")
        if context.user_id != envelope.owner_user_id:
            raise ValueError("context user does not match agent run")
        if context.deal_id != envelope.deal_id:
            raise ValueError("context deal does not match agent run")

    @staticmethod
    def _normalize_output(
        envelope: AgentRunRuntimeEnvelope,
        content: Any,
    ) -> tuple[str | None, Any | None]:
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="json")

        if envelope.output_mode != "json":
            if content is None:
                raise ValueError("agent returned no output")
            return (content if isinstance(content, str) else json.dumps(content, default=str), None)

        if isinstance(content, str):
            try:
                structured = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError("agent output is not valid JSON") from exc
        else:
            structured = content
        if structured is None:
            raise ValueError("agent returned no structured output")
        if envelope.output_schema is not None:
            Draft202012Validator(envelope.output_schema).validate(structured)
        return json.dumps(structured, default=str), structured
