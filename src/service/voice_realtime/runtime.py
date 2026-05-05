from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from src.agents.context import AgentContext
from src.domain.models.user_models import UserRealtimeRuntimeConfig
from src.infra.config.agent_config import agent_config
from src.infra.config.configs import openai_config
from src.service.voice_realtime.state_service import (
    VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS,
    VoiceRealtimeStateService,
)
from src.service.voice_realtime.tool_executor import BackendRealtimeToolExecutor
from src.service.voice_realtime.transport import (
    AiohttpRealtimeSidebandTransport,
    RealtimeSidebandTransport,
)


class VoiceRealtimeSidebandRuntime:
    """Own the live sideband session and handle tool calls for one realtime call."""

    def __init__(
        self,
        *,
        runtime_config: UserRealtimeRuntimeConfig,
        transport: RealtimeSidebandTransport,
        tool_executor: BackendRealtimeToolExecutor,
    ):
        self.runtime_config = runtime_config
        self.transport = transport
        self.tool_executor = tool_executor

    async def run(self) -> None:
        await self.transport.connect()
        try:
            await self.transport.send_event(self.runtime_config.session_update)
            async for event in self.transport.iter_events():
                await self._handle_event(event)
        finally:
            await self.transport.close()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "response.function_call_arguments.done":
            return

        tool_name = str(event.get("name") or "")
        function_call_id = str(event.get("call_id") or event.get("item_id") or "")
        arguments = parse_function_arguments(event.get("arguments"))
        result = await self.tool_executor.execute(tool_name, arguments)

        await self.transport.send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": function_call_id,
                    "output": json.dumps(result.payload),
                },
            }
        )
        await self.transport.send_event({"type": "response.create"})


class VoiceRealtimeSidebandService:
    """Run one live voice sideband session inside the chat worker."""

    def __init__(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        user_email: str | None,
        chat_session_id: str,
        session_id: str,
        call_id: str,
        runtime_config: UserRealtimeRuntimeConfig,
        job_id: UUID | None = None,
        user_timezone: str | None = None,
        transport_factory: Callable[..., RealtimeSidebandTransport] | None = None,
    ):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_email = user_email
        self.chat_session_id = chat_session_id
        self.session_id = session_id
        self.call_id = call_id
        self.runtime_config = runtime_config
        self.user_timezone = user_timezone
        self.transport_factory = transport_factory or create_aiohttp_transport
        self.state_service = VoiceRealtimeStateService(
            organisation_id=organisation_id,
            user_id=user_id,
            chat_session_id=chat_session_id,
            session_id=session_id,
            call_id=call_id,
            job_id=job_id,
        )
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        endpoint = (openai_config.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
        api_key = openai_config.AZURE_OPENAI_API_KEY
        model = agent_config.VOICE_REALTIME_MODEL
        if not endpoint or not api_key or not model:
            await self.state_service.update_call_state(
                status="failed",
                error="Realtime service is not configured.",
            )
            raise RuntimeError("Realtime service is not configured.")

        await self.state_service.update_call_state(status="connecting")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            metadata: dict[str, Any] = {"chat_session_id": self.chat_session_id}
            if self.user_timezone:
                metadata["user_timezone"] = self.user_timezone

            runtime = VoiceRealtimeSidebandRuntime(
                runtime_config=self.runtime_config,
                transport=self.transport_factory(
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    call_id=self.call_id,
                ),
                tool_executor=BackendRealtimeToolExecutor(
                    context=AgentContext(
                        organisation_id=self.organisation_id,
                        user_id=self.user_id,
                        user_email=self.user_email,
                        channel="voice",
                        metadata=metadata,
                    ),
                    allowed_tools=list(self.runtime_config.allowed_tools),
                ),
            )
            await self.state_service.update_call_state(status="active")
            await runtime.run()
            await self.state_service.update_call_state(status="completed")
        except Exception as exc:
            await self.state_service.update_call_state(status="failed", error=str(exc))
            raise
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
                self._heartbeat_task = None
            await asyncio.to_thread(self.state_service.release_lock)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.state_service.refresh_lock)
            await asyncio.sleep(VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS)


def parse_function_arguments(raw_arguments: Any) -> dict[str, Any]:
    if raw_arguments is None:
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def create_aiohttp_transport(**kwargs: Any) -> RealtimeSidebandTransport:
    return AiohttpRealtimeSidebandTransport(**kwargs)


__all__ = [
    "VoiceRealtimeSidebandRuntime",
    "VoiceRealtimeSidebandService",
    "create_aiohttp_transport",
    "parse_function_arguments",
]
