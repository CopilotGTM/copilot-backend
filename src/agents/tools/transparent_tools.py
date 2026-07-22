"""
Transparent tool usage helpers for agents.
Provides visibility into tool usage through SSE events.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from src.infra.messaging.chat_stream_models import ProgressUpdate, ThinkingUpdate, ToolUsage


class TransparentToolManager:
    """Manages transparent tool usage with SSE event streaming."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self._active_tools: dict[str, dict] = {}

    async def _emit_frame(self, frame_emitter: Callable | None, frame: Any) -> None:
        """Emit a frame, handling both sync and async emitters."""
        if frame_emitter is None:
            return
        if asyncio.iscoroutinefunction(frame_emitter):
            await frame_emitter(frame)
        else:
            frame_emitter(frame)

    def _sanitize_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """Create a sanitized copy of parameters safe for JSON serialization.

        Removes or converts non-serializable objects like database sessions.
        """
        sanitized = {}
        for key, value in parameters.items():
            # Skip known non-serializable types
            if (
                key in ("session", "db", "connection")
                or hasattr(value, "__class__")
                and "Session" in type(value).__name__
            ):
                sanitized[key] = f"<{type(value).__name__}>"
            elif isinstance(value, str | int | float | bool | type(None)):
                sanitized[key] = value
            elif isinstance(value, list | tuple):
                sanitized[key] = [
                    str(v)[:100] if not isinstance(v, str | int | float | bool | type(None)) else v
                    for v in value
                ]
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_parameters(value)
            else:
                # Convert other objects to string representation
                sanitized[key] = str(value)[:100]
        return sanitized

    @staticmethod
    def _public_tool_result(result: Any) -> Any:
        """Remove private transport metadata before summarizing tool output."""
        if isinstance(result, dict):
            return {key: value for key, value in result.items() if not str(key).startswith("_")}
        return result

    @staticmethod
    def _tool_result_succeeded(result: Any) -> bool:
        """Classify normal tool returns that encode a handled failure as data."""
        if not isinstance(result, dict):
            return True
        if isinstance(result.get("success"), bool):
            return result["success"]
        status = str(result.get("status") or "").lower()
        if status in {"error", "failed", "failure"}:
            return False
        return not bool(result.get("error"))

    async def use_tool(
        self,
        tool_name: str,
        tool_func: Callable,
        parameters: dict[str, Any],
        description: str | None = None,
        frame_emitter: Callable | None = None,
    ) -> Any:
        """Use a tool transparently with progress tracking."""
        tool_id = str(uuid4())
        start_time = time.time()

        # Sanitize parameters for logging/serialization (remove sessions, etc.)
        safe_parameters = self._sanitize_parameters(parameters)

        # Emit starting frame
        await self._emit_frame(
            frame_emitter,
            ToolUsage(
                message_id=self.message_id,
                tool_name=tool_name,
                tool_description=description,
                status="starting",
                parameters=safe_parameters,
            ),
        )

        try:
            # Store active tool
            self._active_tools[tool_id] = {
                "name": tool_name,
                "start_time": start_time,
                "parameters": safe_parameters,
            }

            # Emit in-progress frame
            await self._emit_frame(
                frame_emitter,
                ToolUsage(
                    message_id=self.message_id,
                    tool_name=tool_name,
                    status="in_progress",
                ),
            )

            # Execute tool
            result = (
                await tool_func(**parameters)
                if asyncio.iscoroutinefunction(tool_func)
                else tool_func(**parameters)
            )

            duration_ms = int((time.time() - start_time) * 1000)
            public_result = self._public_tool_result(result)
            channel_payload = (
                result.get("_bot_channel_payload") if isinstance(result, dict) else None
            )

            # Emit completion frame
            await self._emit_frame(
                frame_emitter,
                ToolUsage(
                    message_id=self.message_id,
                    tool_name=tool_name,
                    status="completed",
                    succeeded=self._tool_result_succeeded(result),
                    result_summary=(
                        str(public_result)[:200] + "..."
                        if len(str(public_result)) > 200
                        else str(public_result)
                    ),
                    channel_payload=channel_payload,
                    duration_ms=duration_ms,
                ),
            )

            return result

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            # Emit failure frame
            await self._emit_frame(
                frame_emitter,
                ToolUsage(
                    message_id=self.message_id,
                    tool_name=tool_name,
                    status="failed",
                    succeeded=False,
                    result_summary=f"Error: {str(e)}",
                    duration_ms=duration_ms,
                ),
            )
            raise
        finally:
            # Clean up
            self._active_tools.pop(tool_id, None)

    async def emit_thinking(
        self,
        thought: str,
        stage: str | None = None,
        frame_emitter: Callable | None = None,
    ):
        """Emit a thinking update."""
        if frame_emitter:
            frame = ThinkingUpdate(message_id=self.message_id, thought=thought, stage=stage)
            if asyncio.iscoroutinefunction(frame_emitter):
                await frame_emitter(frame)
            else:
                frame_emitter(frame)

    async def emit_progress(
        self,
        message: str,
        progress: float | None = None,
        stage: str | None = None,
        eta_seconds: int | None = None,
        frame_emitter: Callable | None = None,
    ):
        """Emit a progress update."""
        if frame_emitter:
            frame = ProgressUpdate(
                message_id=self.message_id,
                message=message,
                stage=stage,
                progress=progress,
                eta_seconds=eta_seconds,
            )
            if asyncio.iscoroutinefunction(frame_emitter):
                await frame_emitter(frame)
            else:
                frame_emitter(frame)


class AgentCapabilities:
    """Enhanced agent capabilities with transparent operations."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.tool_manager = TransparentToolManager(message_id)
        self._frame_emitter: Callable | None = None

    def set_frame_emitter(self, emitter: Callable):
        """Set the frame emitter for transparent operations."""
        self._frame_emitter = emitter

    async def think_aloud(self, thought: str, stage: str | None = None):
        """Share agent thinking process."""
        if self._frame_emitter:
            frame = ThinkingUpdate(message_id=self.message_id, thought=thought, stage=stage)
            if asyncio.iscoroutinefunction(self._frame_emitter):
                await self._frame_emitter(frame)
            else:
                self._frame_emitter(frame)

    async def update_progress(
        self,
        message: str,
        progress: float | None = None,
        stage: str | None = None,
        eta_seconds: int | None = None,
    ):
        """Update task progress."""
        if self._frame_emitter:
            frame = ProgressUpdate(
                message_id=self.message_id,
                message=message,
                stage=stage,
                progress=progress,
                eta_seconds=eta_seconds,
            )
            if asyncio.iscoroutinefunction(self._frame_emitter):
                await self._frame_emitter(frame)
            else:
                self._frame_emitter(frame)

    async def use_tool(
        self,
        tool_name: str,
        tool_func: Callable,
        parameters: dict[str, Any],
        description: str | None = None,
    ) -> Any:
        """Use a tool with full transparency."""
        return await self.tool_manager.use_tool(
            tool_name, tool_func, parameters, description, self._frame_emitter
        )
