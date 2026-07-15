"""Contracts for the Exa-backed agent internet-search boundary."""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agno.tools.function import Function

internet_search_module = importlib.import_module("src.agents.tools.search.internet_search")


class _FakeAsyncClient:
    """Return queued responses while recording the requests sent to Exa."""

    def __init__(self, responses: list[httpx.Response], requests: list[dict[str, Any]], **_) -> None:
        self._responses = responses
        self._requests = requests

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, _method: str, url: str, **kwargs: Any) -> _FakeResponseStream:
        """Record one request and return the next queued provider response."""
        self._requests.append({"url": url, **kwargs})
        return _FakeResponseStream(self._responses.pop(0))


class _FakeResponseStream:
    """Expose a queued httpx response through its streaming context contract."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        await self._response.aclose()


def _response(status_code: int, *, json: object = None, headers: dict[str, str] | None = None):
    request = httpx.Request("POST", "https://api.exa.ai/search")
    return httpx.Response(status_code, json=json, headers=headers, request=request)


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx.Response],
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _FakeAsyncClient:
        """Build a fake client sharing this test's response and request lists."""
        return _FakeAsyncClient(responses, requests, **kwargs)

    monkeypatch.setattr(internet_search_module.httpx, "AsyncClient", factory)
    return requests


@pytest.mark.asyncio
async def test_search_sends_agent_defaults_and_normalizes_highlights(monkeypatch) -> None:
    """Generic searches use highlights and return the stable citation shape."""
    # Generic agent searches must omit restrictive entity categories while still
    # returning Exa evidence through the provider-neutral citation contract.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="test-key"),
    )
    requests = _install_client(
        monkeypatch,
        [
            _response(
                200,
                json={
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.com",
                            "highlights": ["First fact", "Second fact"],
                        },
                        {"title": "Missing URL", "highlights": ["ignored"]},
                        "malformed",
                    ]
                },
            )
        ],
    )

    result = await internet_search_module.internet_search(" current facts ")

    assert result == [
        {
            "label": "Example",
            "url": "https://example.com",
            "snippet": "First fact\nSecond fact",
        }
    ]
    assert requests == [
        {
            "url": "https://api.exa.ai/search",
            "json": {
                "query": "current facts",
                "type": "auto",
                "numResults": 5,
                "contents": {"highlights": True},
                "moderation": True,
                "userLocation": "US",
            },
            "headers": {"x-api-key": "test-key", "Content-Type": "application/json"},
        }
    ]


@pytest.mark.asyncio
async def test_search_clamps_results_and_passes_people_category(monkeypatch) -> None:
    """Entity categories are opt-in and cannot bypass the result ceiling."""
    # Dedicated indexes are opt-in, and the old 20-result ceiling remains the
    # cost and context-size boundary even though Exa permits larger requests.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="test-key"),
    )
    requests = _install_client(monkeypatch, [_response(200, json={"results": []})])

    await internet_search_module.internet_search(
        "distributed systems engineer",
        max_results=100,
        category="people",
        region="uk-en",
    )

    assert requests[0]["json"]["numResults"] == 20
    assert requests[0]["json"]["category"] == "people"
    assert requests[0]["json"]["userLocation"] == "GB"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "   "},
        {"query": "query", "category": "news"},
        {"query": "query", "max_results": "five"},
        {"query": "query", "region": []},
        {"query": "query", "timeout": 0},
        {"query": "x" * (internet_search_module._MAX_QUERY_CHARS + 1)},
    ],
)
async def test_invalid_input_does_not_load_credentials(monkeypatch, kwargs) -> None:
    """Invalid tool arguments stop before secrets or provider calls."""
    # Validation happens before credential or provider access so malformed tool
    # arguments neither consume quota nor trigger an unnecessary vault lookup.
    get_api_key = AsyncMock(return_value="test-key")
    monkeypatch.setattr(internet_search_module.exa_config, "get_api_key", get_api_key)

    result = await internet_search_module.internet_search(**kwargs)

    assert result == []
    get_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_api_key_returns_empty_without_http_request(monkeypatch) -> None:
    """Missing deployment credentials degrade search without crashing agents."""
    # A worker can start before its deployment secret is provisioned; search
    # degrades to unavailable instead of crashing the parent agent workflow.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value=None),
    )
    client = AsyncMock(side_effect=AssertionError("HTTP should not be called"))
    monkeypatch.setattr(internet_search_module.httpx, "AsyncClient", client)

    assert await internet_search_module.internet_search("query") == []
    client.assert_not_called()


@pytest.mark.asyncio
async def test_transient_errors_retry_and_honor_retry_after(monkeypatch) -> None:
    """Transient Exa failures receive bounded, Retry-After-aware retries."""
    # Exa's 10-QPS boundary can throttle parallel agent tools, so a bounded
    # Retry-After-aware retry prevents a brief burst from looking like no data.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="test-key"),
    )
    requests = _install_client(
        monkeypatch,
        [
            _response(429, json={"error": "limited"}, headers={"Retry-After": "0.25"}),
            _response(503, json={"error": "unavailable"}),
            _response(200, json={"results": []}),
        ],
    )
    sleep = AsyncMock()
    monkeypatch.setattr(internet_search_module.asyncio, "sleep", sleep)

    assert await internet_search_module.internet_search("query") == []
    assert len(requests) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 1.0]


@pytest.mark.asyncio
async def test_authentication_error_is_not_retried(monkeypatch) -> None:
    """Non-transient credential failures return immediately."""
    # Authentication failures need operator action; retrying them would spend
    # latency without changing the outcome and could obscure the real status.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="bad-key"),
    )
    refresh = AsyncMock(return_value="bad-key")
    monkeypatch.setattr(internet_search_module.exa_config, "refresh_rejected_api_key", refresh)
    requests = _install_client(monkeypatch, [_response(401, json={"error": "invalid"})])

    assert await internet_search_module.internet_search("query") == []
    assert len(requests) == 1
    refresh.assert_awaited_once_with("bad-key")


@pytest.mark.asyncio
async def test_rejected_vault_key_refreshes_once(monkeypatch) -> None:
    """A rotated vault credential is retried once with its replacement."""
    # Shared keys can rotate while workers retain a six-hour cache. A 401 should
    # recover immediately without applying credential retries to other errors.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="old-key"),
    )
    refresh = AsyncMock(return_value="new-key")
    monkeypatch.setattr(internet_search_module.exa_config, "refresh_rejected_api_key", refresh)
    requests = _install_client(
        monkeypatch,
        [
            _response(401, json={"error": "invalid"}),
            _response(200, json={"results": []}),
        ],
    )

    assert await internet_search_module.internet_search("query") == []
    assert [request["headers"]["x-api-key"] for request in requests] == ["old-key", "new-key"]
    refresh.assert_awaited_once_with("old-key")


@pytest.mark.asyncio
async def test_oversized_response_is_rejected(monkeypatch) -> None:
    """Provider bodies over the worker safety ceiling never reach JSON parsing."""
    # A forged or anomalous Content-Length must fail closed before reading the
    # body; otherwise one public search could exhaust a light worker's memory.
    monkeypatch.setattr(
        internet_search_module.exa_config,
        "get_api_key",
        AsyncMock(return_value="test-key"),
    )
    _install_client(
        monkeypatch,
        [
            _response(
                200,
                json={"results": []},
                headers={
                    "Content-Length": str(internet_search_module._MAX_RESPONSE_BYTES + 1)
                },
            )
        ],
    )

    assert await internet_search_module.internet_search("query") == []


def test_normalizer_rejects_unsafe_urls_and_bounds_agent_context() -> None:
    """Only bounded, public HTTP(S) citations enter the agent context."""
    # Search results are untrusted provider data. Reject schemes and hosts that
    # could become script injection or SSRF inputs if an agent crawls the URL.
    results = internet_search_module._normalize_results(
        [
            {"url": "javascript:alert(1)", "title": "script"},
            {"url": "http://127.0.0.1/admin", "title": "loopback"},
            {"url": "http://metadata.internal/token", "title": "internal"},
            {"url": "https://user:password@example.com", "title": "credentials"},
            {
                "url": "https://example.com/article",
                "title": "t" * (internet_search_module._MAX_LABEL_CHARS + 10),
                "highlights": ["s" * (internet_search_module._MAX_SNIPPET_CHARS + 10)],
            },
        ]
    )

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/article"
    assert len(results[0]["label"]) == internet_search_module._MAX_LABEL_CHARS
    assert len(results[0]["snippet"]) == internet_search_module._MAX_SNIPPET_CHARS


@pytest.mark.parametrize(
    "header, expected",
    [("nan", 0.5), ("inf", 0.5), ("100", 5.0), ("-2", 0.0), ("invalid", 0.5)],
)
def test_retry_after_is_finite_and_bounded(header, expected) -> None:
    """Untrusted Retry-After values cannot create an unlimited agent wait."""
    response = _response(429, headers={"Retry-After": header})

    assert internet_search_module._retry_delay(response, 0) == expected


def test_agent_schema_exposes_only_supported_optional_categories() -> None:
    """Agno publishes the intended category enum without making it required."""
    # This checks the real tool-schema builder, not only Python annotations; a
    # schema regression would prevent models from calling the new interface.
    tool = internet_search_module.create_search_internet(lambda: None)

    parameters = Function.from_callable(tool).parameters

    assert parameters["required"] == ["query"]
    assert parameters["properties"]["category"] == {
        "type": "string",
        "enum": ["people", "company"],
        "description": "Optional ``people`` or ``company`` dedicated index.",
    }
