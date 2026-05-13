"""Verify worker Graph wrappers keep GET vs POST transport behavior intact.

`graph_api_request` must preserve query encoding and body rules for GET calls,
while `graph_api_post_json` must send JSON bodies and not reuse GET serialization.
"""

from __future__ import annotations

import httpx
import pytest
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_post_json,
    graph_api_post_no_content,
    graph_api_request,
)


@pytest.mark.asyncio
async def test_graph_api_request_preserves_existing_get_params_behavior() -> None:
    """GET wrapper must preserve query params and transport semantics."""
    # Contract: query params and method must pass through unchanged for GET helper.
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """Capture method/path/query/body assertions for the GET wrapper."""
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"value": [{"id": "item-1"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await graph_api_request(
            client,
            "/v1.0/me/messages",
            params={"$top": 1, "$select": "id"},
        )

    assert result == {"value": [{"id": "item-1"}]}
    assert captured["method"] == "GET"
    assert captured["path"] == "/v1.0/me/messages"
    assert captured["query"] == "%24top=1&%24select=id"
    assert captured["body"] == ""


@pytest.mark.asyncio
async def test_graph_api_post_json_supports_post_json_body_without_changing_get_helper() -> None:
    """POST wrapper must send body and not inherit GET wrapper behavior."""
    # Contract: POST helper uses body semantics and does not share GET behavior.
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """Capture method/path/body assertions for the POST wrapper."""
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"id": "event-1"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await graph_api_post_json(
            client,
            "/v1.0/me/events",
            json_body={"subject": "Discovery call"},
        )

    assert result == {"id": "event-1"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1.0/me/events"
    assert captured["body"] == '{"subject":"Discovery call"}'


@pytest.mark.asyncio
async def test_graph_api_post_no_content_accepts_empty_accepted_response() -> None:
    """POST no-content helper should accept Graph 202 responses with no JSON body."""
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(202, content=b"")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await graph_api_post_no_content(
            client,
            "/v1.0/me/sendMail",
            json_body={"message": {"subject": "Hello"}},
        )

    assert result is None
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1.0/me/sendMail"
    assert captured["body"] == '{"message":{"subject":"Hello"}}'


@pytest.mark.asyncio
async def test_graph_api_post_no_content_does_not_retry_by_default() -> None:
    """Non-idempotent no-content writes such as sendMail must not auto-retry."""
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, request=request, text="rate limited")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await graph_api_post_no_content(
                client,
                "/v1.0/me/sendMail",
                json_body={"message": {"subject": "Hello"}},
            )

    assert attempts == 1
