from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName
from src.infra.utils.logger import get_logger
from src.service.voice_realtime.tool_manifest import build_voice_tool_factories

logger = get_logger(__name__)


@dataclass(slots=True)
class ToolExecutionResult:
    ok: bool
    payload: dict[str, Any]


class BackendRealtimeToolExecutor:
    """Execute approved realtime tools on the backend with org-scoped context."""

    def __init__(self, *, context: AgentContext, allowed_tools: list[str]):
        self.context = context
        self.allowed_tools = set(allowed_tools)
        self.factories = build_voice_tool_factories(context)

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        if tool_name not in self.allowed_tools:
            return self._error("tool_not_allowed", f"Tool '{tool_name}' is not allowed.")

        try:
            tool_enum = ToolName(tool_name)
        except ValueError:
            return self._error("unknown_tool", f"Tool '{tool_name}' is not registered.")

        factory = self.factories.get(tool_enum)
        if factory is None:
            return self._error("tool_unavailable", f"Tool '{tool_name}' is unavailable.")
        tool_callable = factory()

        try:
            kwargs = self._prepare_kwargs(tool_callable, arguments)
        except ValueError as exc:
            return self._error("invalid_arguments", str(exc))

        try:
            result = tool_callable(**kwargs)
            if isinstance(result, Awaitable):
                result = await result
        except Exception as exc:  # pragma: no cover
            logger.exception("Realtime tool execution failed for %s", tool_name)
            return self._error("tool_execution_failed", str(exc))

        return ToolExecutionResult(ok=True, payload={"ok": True, "data": jsonable_encoder(result)})

    def _prepare_kwargs(
        self, tool_callable: Callable[..., Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        signature = inspect.signature(tool_callable)
        accepted_names = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        kwargs = {name: value for name, value in arguments.items() if name in accepted_names}
        missing = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and parameter.default is inspect.Signature.empty
            and name not in kwargs
        ]
        if missing:
            raise ValueError(f"Missing required arguments: {', '.join(sorted(missing))}")
        return kwargs

    @staticmethod
    def _error(code: str, message: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            ok=False,
            payload={"ok": False, "error": {"code": code, "message": message}},
        )


__all__ = [
    "BackendRealtimeToolExecutor",
    "ToolExecutionResult",
]
