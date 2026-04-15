from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


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
        websocket_url = build_sideband_websocket_url(
            endpoint=self.endpoint,
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


def build_sideband_websocket_url(*, endpoint: str, call_id: str) -> str:
    if endpoint.startswith("https://"):
        websocket_base = "wss://" + endpoint[len("https://") :]
    elif endpoint.startswith("http://"):
        websocket_base = "ws://" + endpoint[len("http://") :]
    else:
        websocket_base = endpoint

    # Attached-call sideband control targets the existing live call by call_id.
    # Adding model here can open a separate session instead of observing the live call.
    return f"{websocket_base}/openai/v1/realtime?call_id={quote(call_id)}"


__all__ = [
    "AiohttpRealtimeSidebandTransport",
    "RealtimeSidebandTransport",
    "build_sideband_websocket_url",
]
