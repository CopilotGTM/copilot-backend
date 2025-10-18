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

from src.api.schemas.chat_stream_models import ProgressUpdate, ThinkingUpdate, ToolUsage


class TransparentToolManager:
    """Manages transparent tool usage with SSE event streaming."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self._active_tools: dict[str, dict] = {}

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

        # Emit starting frame
        if frame_emitter:
            await frame_emitter(
                ToolUsage(
                    message_id=self.message_id,
                    tool_name=tool_name,
                    tool_description=description,
                    status="starting",
                    parameters=parameters,
                )
            )

        try:
            # Store active tool
            self._active_tools[tool_id] = {
                "name": tool_name,
                "start_time": start_time,
                "parameters": parameters,
            }

            # Emit in-progress frame
            if frame_emitter:
                await frame_emitter(
                    ToolUsage(
                        message_id=self.message_id,
                        tool_name=tool_name,
                        status="in_progress",
                    )
                )

            # Execute tool
            result = (
                await tool_func(**parameters)
                if asyncio.iscoroutinefunction(tool_func)
                else tool_func(**parameters)
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Emit completion frame
            if frame_emitter:
                await frame_emitter(
                    ToolUsage(
                        message_id=self.message_id,
                        tool_name=tool_name,
                        status="completed",
                        result_summary=(
                            str(result)[:200] + "..." if len(str(result)) > 200 else str(result)
                        ),
                        duration_ms=duration_ms,
                    )
                )

            return result

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            # Emit failure frame
            if frame_emitter:
                await frame_emitter(
                    ToolUsage(
                        message_id=self.message_id,
                        tool_name=tool_name,
                        status="failed",
                        result_summary=f"Error: {str(e)}",
                        duration_ms=duration_ms,
                    )
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
