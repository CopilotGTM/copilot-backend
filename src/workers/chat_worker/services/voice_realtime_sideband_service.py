from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import aiohttp
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlmodel import col

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.domain.models.user_models import RealtimeControlMode, UserRealtimeRuntimeConfig
from src.infra.config.agent_config import agent_config
from src.infra.config.configs import openai_config
from src.infra.database.sessions import acquire_org_async_session, master_async_session_scope
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import load_organisation_runtime_context

logger = get_logger(__name__)

_VOICE_REALTIME_LOCK_TTL_SECONDS = 120
_VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS = 30


def voice_realtime_lock_key(call_id: str) -> str:
    return f"voice_realtime:call:{call_id}"


class RealtimeSidebandTransport(Protocol):
    async def connect(self) -> None: ...

    async def send_event(self, event: dict[str, Any]) -> None: ...

    def iter_events(self) -> AsyncGenerator[dict[str, Any], None]: ...

    async def close(self) -> None: ...


class AiohttpRealtimeSidebandTransport:
    """WebSocket transport for backend sideband control of a live realtime call."""

    def __init__(self, *, endpoint: str, api_key: str, model: str, call_id: str):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.call_id = call_id
        self._client_session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None

    async def connect(self) -> None:
        websocket_url = _build_sideband_websocket_url(
            endpoint=self.endpoint,
            model=self.model,
            call_id=self.call_id,
        )
        self._client_session = aiohttp.ClientSession()
        self._websocket = await self._client_session.ws_connect(
            websocket_url,
            headers={"api-key": self.api_key},
            heartbeat=30,
        )

    async def send_event(self, event: dict[str, Any]) -> None:
        if self._websocket is None:
            raise RuntimeError("Realtime sideband transport is not connected")
        await self._websocket.send_json(event)

    async def iter_events(self) -> AsyncGenerator[dict[str, Any], None]:
        if self._websocket is None:
            raise RuntimeError("Realtime sideband transport is not connected")

        async for message in self._websocket:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    yield json.loads(message.data)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON realtime sideband event")
            elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                break
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError("Realtime sideband websocket reported an error")

    async def close(self) -> None:
        if self._websocket is not None:
            await self._websocket.close()
            self._websocket = None
        if self._client_session is not None:
            await self._client_session.close()
            self._client_session = None


def _build_sideband_websocket_url(*, endpoint: str, model: str, call_id: str) -> str:
    if endpoint.startswith("https://"):
        websocket_base = "wss://" + endpoint[len("https://") :]
    elif endpoint.startswith("http://"):
        websocket_base = "ws://" + endpoint[len("http://") :]
    else:
        websocket_base = endpoint

    return f"{websocket_base}/openai/v1/realtime?model={quote(model)}&call_id={quote(call_id)}"


@dataclass(slots=True)
class ToolExecutionResult:
    ok: bool
    payload: dict[str, Any]


class BackendRealtimeToolExecutor:
    """Execute approved realtime tools on the backend with org-scoped context."""

    def __init__(self, *, context: AgentContext, allowed_tools: list[str]):
        self.context = context
        self.allowed_tools = set(allowed_tools)
        self.factories = _build_voice_tool_factories(context)

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
            missing_str = ", ".join(sorted(missing))
            raise ValueError(f"Missing required arguments: {missing_str}")
        return kwargs

    @staticmethod
    def _error(code: str, message: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            ok=False,
            payload={"ok": False, "error": {"code": code, "message": message}},
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
            if self.runtime_config.organisation_context_item:
                await self.transport.send_event(self.runtime_config.organisation_context_item)
            async for event in self.transport.iter_events():
                await self._handle_event(event)
        finally:
            await self.transport.close()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "response.function_call_arguments.done":
            return

        tool_name = str(event.get("name") or "")
        function_call_id = str(event.get("call_id") or event.get("item_id") or "")
        arguments = _parse_function_arguments(event.get("arguments"))
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
        transport_factory: Callable[..., RealtimeSidebandTransport] | None = None,
    ):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_email = user_email
        self.chat_session_id = chat_session_id
        self.session_id = session_id
        self.call_id = call_id
        self.runtime_config = runtime_config
        self.transport_factory = transport_factory or _create_aiohttp_transport
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        endpoint = (openai_config.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
        api_key = openai_config.AZURE_OPENAI_API_KEY
        model = agent_config.VOICE_REALTIME_MODEL
        if not endpoint or not api_key or not model:
            await self._update_call_state(
                status="failed",
                error="Realtime service is not configured.",
            )
            raise RuntimeError("Realtime service is not configured.")

        await self._update_call_state(status="connecting")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            async with master_async_session_scope() as master_session:
                org_name, organisation_profile = await load_organisation_runtime_context(
                    master_session, self.organisation_id
                )
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
                        org_name=org_name or "Our Organization",
                        organisation_profile=organisation_profile,
                        user_id=self.user_id,
                        user_email=self.user_email,
                        channel="voice",
                        metadata={"chat_session_id": self.chat_session_id},
                    ),
                    allowed_tools=list(self.runtime_config.allowed_tools),
                ),
            )
            await self._update_call_state(status="active")
            await runtime.run()
            await self._update_call_state(status="completed")
        except Exception as exc:
            await self._update_call_state(status="failed", error=str(exc))
            raise
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
                self._heartbeat_task = None
            await asyncio.to_thread(self._release_lock)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.to_thread(self._refresh_lock)
            await asyncio.sleep(_VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS)

    def _refresh_lock(self) -> None:
        redis_client = get_redis_client()
        redis_client.set(
            voice_realtime_lock_key(self.call_id),
            self.chat_session_id,
            ex=_VOICE_REALTIME_LOCK_TTL_SECONDS,
        )

    def _release_lock(self) -> None:
        redis_client = get_redis_client()
        lock_key = voice_realtime_lock_key(self.call_id)
        if redis_client.get(lock_key) == self.chat_session_id:
            redis_client.delete(lock_key)

    async def _update_call_state(self, *, status: str, error: str | None = None) -> None:
        async for db in acquire_org_async_session(self.organisation_id):
            result = await db.execute(
                select(ChatSession).where(
                    col(ChatSession.session_id) == UUID(self.chat_session_id),
                    col(ChatSession.user_id) == self.user_id,
                    col(ChatSession.organisation_id) == self.organisation_id,
                    col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                    col(ChatSession.is_deleted).is_(False),
                )
            )
            chat_session = result.scalars().one_or_none()
            if chat_session is None:
                logger.warning(
                    "Voice realtime chat session disappeared before state update: %s",
                    self.chat_session_id,
                )
                return

            properties = dict(chat_session.properties or {})
            properties.update(
                {
                    "realtime_session_id": self.session_id,
                    "realtime_call_id": self.call_id,
                    "realtime_control_mode": RealtimeControlMode.BACKEND_SIDEBAND.value,
                    "voice_sideband_status": status,
                }
            )
            if status == "queued":
                properties.setdefault("voice_sideband_queued_at", _utcnow_iso())
            if status in {"connecting", "active"} and not properties.get(
                "voice_sideband_started_at"
            ):
                properties["voice_sideband_started_at"] = _utcnow_iso()
            if status in {"completed", "failed"}:
                properties["voice_sideband_ended_at"] = _utcnow_iso()
            if error:
                properties["voice_sideband_error"] = error
            elif status != "failed":
                properties.pop("voice_sideband_error", None)

            chat_session.properties = properties
            db.add(chat_session)
            await db.commit()
            return


def _parse_function_arguments(raw_arguments: Any) -> dict[str, Any]:
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


def _build_voice_tool_factories(
    context: AgentContext,
) -> dict[ToolName, Callable[[], Callable[..., Any]]]:
    from src.agents.tools.communication.get_email_details import create_get_email_details
    from src.agents.tools.communication.list_emails import create_list_deal_emails
    from src.agents.tools.crm.create_deal import create_create_deal
    from src.agents.tools.crm.get_deal_details import create_get_deal_details
    from src.agents.tools.crm.get_deal_summary import create_get_deal_summary
    from src.agents.tools.crm.get_risks_blockers import create_get_existing_risks_blockers
    from src.agents.tools.crm.list_deal_notes import create_list_deal_notes
    from src.agents.tools.crm.list_deals import create_list_deals
    from src.agents.tools.crm.search_contacts import create_search_contacts
    from src.agents.tools.crm.search_deals import create_search_deals
    from src.agents.tools.intelligence.get_meeting_details import create_get_meeting_details
    from src.agents.tools.intelligence.get_meeting_summary import create_get_meeting_summary
    from src.agents.tools.intelligence.get_meeting_transcript import create_get_meeting_transcript
    from src.agents.tools.intelligence.list_meetings import create_list_deal_meetings
    from src.agents.tools.intelligence.list_meetings_by_date import create_list_meetings_by_date
    from src.agents.tools.search.internal_knowledge_base import create_search_internal_knowledge

    def empty_capabilities() -> None:
        return None

    return {
        ToolName.SEARCH_INTERNAL_KNOWLEDGE: partial(
            create_search_internal_knowledge,
            context,
            empty_capabilities,
        ),
        ToolName.SEARCH_DEALS: partial(create_search_deals, context, empty_capabilities),
        ToolName.LIST_DEALS: partial(create_list_deals, context, empty_capabilities),
        ToolName.CREATE_DEAL: partial(create_create_deal, context, empty_capabilities),
        ToolName.GET_DEAL_DETAILS: partial(create_get_deal_details, context, empty_capabilities),
        ToolName.GET_DEAL_SUMMARY: partial(create_get_deal_summary, context, empty_capabilities),
        ToolName.GET_EXISTING_RISKS_BLOCKERS: partial(
            create_get_existing_risks_blockers,
            context,
            empty_capabilities,
        ),
        ToolName.LIST_DEAL_NOTES: partial(create_list_deal_notes, context, empty_capabilities),
        ToolName.SEARCH_CONTACTS: partial(create_search_contacts, context, empty_capabilities),
        ToolName.LIST_DEAL_MEETINGS: partial(
            create_list_deal_meetings,
            context,
            empty_capabilities,
        ),
        ToolName.LIST_MEETINGS_BY_DATE: partial(
            create_list_meetings_by_date,
            context,
            empty_capabilities,
        ),
        ToolName.GET_MEETING_DETAILS: partial(
            create_get_meeting_details,
            context,
            empty_capabilities,
        ),
        ToolName.GET_MEETING_SUMMARY: partial(
            create_get_meeting_summary,
            context,
            empty_capabilities,
        ),
        ToolName.GET_MEETING_TRANSCRIPT: partial(
            create_get_meeting_transcript,
            context,
            empty_capabilities,
        ),
        ToolName.LIST_DEAL_EMAILS: partial(
            create_list_deal_emails,
            context,
            empty_capabilities,
        ),
        ToolName.GET_EMAIL_DETAILS: partial(
            create_get_email_details,
            context,
            empty_capabilities,
        ),
    }


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _create_aiohttp_transport(**kwargs: Any) -> RealtimeSidebandTransport:
    return AiohttpRealtimeSidebandTransport(**kwargs)


__all__ = [
    "BackendRealtimeToolExecutor",
    "ToolExecutionResult",
    "VoiceRealtimeSidebandRuntime",
    "VoiceRealtimeSidebandService",
    "voice_realtime_lock_key",
]
