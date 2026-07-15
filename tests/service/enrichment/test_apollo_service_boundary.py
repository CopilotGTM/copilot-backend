"""Boundary tests for the shared Apollo client and its provider safety limits."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def test_apollo_enrichment_is_exposed_from_service_package() -> None:
    """CRM callers should use the stable service package rather than an agent module."""
    from src.service.enrichment.apollo import apollo_enrich_organization, apollo_enrich_person

    assert callable(apollo_enrich_person)
    assert callable(apollo_enrich_organization)


def test_application_code_imports_apollo_from_service_package() -> None:
    """Removed agent-local Apollo imports must not reappear in application code."""
    forbidden = "src.agents.tools.research.apollo_client"
    repo_root = Path(__file__).resolve().parents[3]
    offenders: list[str] = []

    removed_client = repo_root / "src" / "agents" / "tools" / "research" / "apollo_client.py"
    assert not removed_client.exists()

    for path in (repo_root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []


@pytest.mark.asyncio
async def test_apollo_response_reader_rejects_streams_over_the_memory_limit() -> None:
    """Provider response limits must apply while streaming, before JSON decoding."""
    from src.service.enrichment.apollo.client import (
        MAX_APOLLO_RESPONSE_BYTES,
        ApolloResponseTooLargeError,
        _read_bounded_json_response,
    )

    class _OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            """Yield chunked content without a Content-Length header."""
            yield b"x" * MAX_APOLLO_RESPONSE_BYTES
            yield b"x"

    response = httpx.Response(200, stream=_OversizedStream())

    with pytest.raises(ApolloResponseTooLargeError):
        await _read_bounded_json_response(response)
