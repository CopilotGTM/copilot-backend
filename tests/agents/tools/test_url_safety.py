"""Security contracts for URLs accepted by public-network agent tools."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from src.agents.tools.url_safety import is_safe_public_http_url, resolves_to_public_network


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://service.internal/token",
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "https://user:password@example.com",
        "https://example.com:99999",
        "https://example.com/path with spaces",
    ],
)
def test_non_public_or_malformed_urls_are_rejected(url: str) -> None:
    """Unsafe schemes, local targets, credentials, and malformed URLs fail closed."""
    assert not is_safe_public_http_url(url)


@pytest.mark.parametrize("url", ["https://example.com", "http://8.8.8.8/search"])
def test_plausibly_public_urls_pass_syntax_validation(url: str) -> None:
    """Normal public HTTP(S) URLs remain available to research agents."""
    assert is_safe_public_http_url(url)


@pytest.mark.asyncio
async def test_dns_answer_with_any_private_address_is_rejected(monkeypatch) -> None:
    """Mixed public/private DNS answers fail closed to reduce rebinding risk."""
    # A hostname is unsafe if any current answer can route Chromium to an
    # internal service, even when another answer is globally reachable.
    loop = asyncio.get_running_loop()
    getaddrinfo = AsyncMock(
        return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
    )
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)

    assert not await resolves_to_public_network("https://example.com")


@pytest.mark.asyncio
async def test_dns_resolution_requires_at_least_one_global_answer(monkeypatch) -> None:
    """A hostname with only public answers passes the network check."""
    loop = asyncio.get_running_loop()
    getaddrinfo = AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)

    assert await resolves_to_public_network("https://example.com")
