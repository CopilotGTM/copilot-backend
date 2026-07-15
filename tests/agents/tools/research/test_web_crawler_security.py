"""Security checks for browser requests made by the agent web crawler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.agents.tools.research import web_crawler


class _Route:
    """Record whether the crawler continued or aborted one browser request."""

    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.continue_ = AsyncMock()
        self.abort = AsyncMock()


class _Page:
    """Capture the Playwright route handler installed by the crawler hook."""

    def __init__(self) -> None:
        self.pattern: str | None = None
        self.handler = None

    async def route(self, pattern, handler) -> None:
        """Store the interception callback for direct security assertions."""
        self.pattern = pattern
        self.handler = handler


@pytest.mark.asyncio
async def test_browser_route_aborts_private_subresources(monkeypatch) -> None:
    """Redirects and page subresources are rechecked before Chromium loads them."""
    # Initial URL validation is insufficient because a public page can redirect
    # or embed a request to cloud metadata after navigation starts.
    resolves = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(web_crawler, "resolves_to_public_network", resolves)
    page = _Page()
    await web_crawler._guard_browser_requests(page)

    public_route = _Route("https://example.com/style.css")
    private_route = _Route("http://169.254.169.254/latest/meta-data")
    await page.handler(public_route)
    await page.handler(private_route)

    public_route.continue_.assert_awaited_once()
    public_route.abort.assert_not_awaited()
    private_route.abort.assert_awaited_once()
    private_route.continue_.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_initial_resolution_stops_before_browser_import(monkeypatch) -> None:
    """Private DNS answers prevent browser startup and all outbound requests."""
    resolves = AsyncMock(return_value=False)
    monkeypatch.setattr(web_crawler, "resolves_to_public_network", resolves)

    assert await web_crawler.crawl_webpage("https://example.com") == {}
    resolves.assert_awaited_once_with("https://example.com")
