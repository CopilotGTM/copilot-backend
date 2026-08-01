"""Org-scoped execution adapter for Azure Realtime function calls.

The voice sideband runtime calls this module after the provider selects an
allowlisted function. Results are reduced to provider-safe JSON so chat-channel
metadata and unsupported binary media never enter the live model conversation.
"""

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

_PRIVATE_TRANSPORT_KEYS = {"_bot_channel_payload"}


def _strip_private_transport_metadata(value: Any) -> Any:
    """Remove channel-only payloads before a result enters the provider conversation."""
    if isinstance(value, dict):
        return {
            key: _strip_private_transport_metadata(item)
            for key, item in value.items()
            if key not in _PRIVATE_TRANSPORT_KEYS
        }
    if isinstance(value, list):
        return [_strip_private_transport_metadata(item) for item in value]
    return value


def _realtime_safe_result(result: Any) -> Any:
    """Convert a backend result to JSON without leaking bytes or bot-channel metadata."""
    if getattr(result, "images", None):
        return {
            "status": "unsupported",
            "code": "voice_image_attachment_unsupported",
            "message": "Image attachments cannot be analyzed in the realtime voice session.",
        }
    return _strip_private_transport_metadata(jsonable_encoder(result))


@dataclass(slots=True)
class ToolExecutionResult:
    """Provider-safe function output plus a success flag for runtime handling."""

    ok: bool
    payload: dict[str, Any]


class BackendRealtimeToolExecutor:
    """Execute approved realtime tools on the backend with org-scoped context."""

    def __init__(self, *, context: AgentContext, allowed_tools: list[str]):
        self.context = context
        self.allowed_tools = set(allowed_tools)
        self.factories = build_voice_tool_factories(context)

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Execute one allowlisted tool inside the immutable authenticated context."""
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

        return ToolExecutionResult(
            ok=True, payload={"ok": True, "data": _realtime_safe_result(result)}
        )

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
