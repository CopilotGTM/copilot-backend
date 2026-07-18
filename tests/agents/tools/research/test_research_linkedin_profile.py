"""Regression tests for tenant-safe, email-first professional research."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from src.agents.context import AgentContext
from src.agents.tools.research.research_linkedin_profile import research_linkedin_profile

research_module = importlib.import_module("src.agents.tools.research.research_linkedin_profile")


class _AsyncSessionContext:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalars(self) -> _ScalarResult:
        """Mirror SQLAlchemy's scalar-result chaining for the cache lookup."""
        return self

    def first(self) -> Any:
        """Return the configured contact as the exact-email query result."""
        return self.value


class _Session:
    def __init__(self, contact: Any = None) -> None:
        self.contact = contact
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _ScalarResult:
        """Capture the tenant-scoped lookup without requiring a test database."""
        self.statements.append(stmt)
        return _ScalarResult(self.contact)


def _context(session: _Session) -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    return context


@pytest.mark.asyncio
async def test_cached_email_research_skips_apollo_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact tenant contact cache must win before any paid provider work."""
    enriched_at = datetime(2026, 7, 1, tzinfo=UTC)
    contact = SimpleNamespace(
        contact_research="Enterprise buyer with telecom experience.",
        profile_enrichment={"name": "Buyer", "_raw": {"private": True}},
        last_researched_at=enriched_at,
        profile_enriched_at=enriched_at,
        linkedin_url="https://www.linkedin.com/in/buyer",
    )
    context = _context(_Session(contact))
    apollo = AsyncMock()
    limiter = MagicMock()
    monkeypatch.setattr(research_module, "get_internal_domains", AsyncMock(return_value={"seller.example"}))
    monkeypatch.setattr(research_module, "apollo_enrich_person", apollo)
    monkeypatch.setattr(research_module, "enforce_contact_research_rate_limit", limiter)

    result = await research_linkedin_profile(email="BUYER@customer.example", context=context)

    assert result["source"] == "crm_cache"
    assert result["profile"] == {"name": "Buyer"}
    assert result["enriched_at"] == enriched_at.isoformat()
    apollo.assert_not_awaited()
    limiter.assert_not_called()


@pytest.mark.asyncio
async def test_external_email_without_contact_calls_sanitized_apollo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-contact attendee remains researchable without persisting provider evidence."""
    session = _Session()
    context = _context(session)
    apollo = AsyncMock(
        return_value={
            "success": True,
            "email": "buyer@customer.example",
            "name": "Buyer",
            "linkedin_url": "https://www.linkedin.com/in/buyer",
            "raw": {"sensitive": "provider payload"},
        }
    )
    limiter = MagicMock()
    monkeypatch.setattr(research_module, "get_internal_domains", AsyncMock(return_value={"seller.example"}))
    monkeypatch.setattr(research_module, "apollo_enrich_person", apollo)
    monkeypatch.setattr(research_module, "enforce_contact_research_rate_limit", limiter)

    result = await research_linkedin_profile(email="buyer@customer.example", context=context)

    assert result["source"] == "apollo"
    assert "raw" not in result["profile"]
    apollo.assert_awaited_once_with(
        email="buyer@customer.example",
        organization_name=None,
    )
    limiter.assert_called_once_with(str(context.user_id))
    assert len(session.statements) == 1
    assert "crm_contacts.organisation_id" in str(session.statements[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("domains", [set(), {"seller.example"}])
async def test_internal_or_unknown_email_never_reaches_apollo(
    monkeypatch: pytest.MonkeyPatch,
    domains: set[str],
) -> None:
    """Missing classification and known internal domains both fail before Apollo."""
    context = _context(_Session())
    apollo = AsyncMock()
    monkeypatch.setattr(research_module, "get_internal_domains", AsyncMock(return_value=domains))
    monkeypatch.setattr(research_module, "apollo_enrich_person", apollo)

    email = "seller@seller.example"
    result = await research_linkedin_profile(email=email, context=context)

    assert result["success"] is False
    apollo.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_requires_authenticated_context() -> None:
    """Email enrichment must not degrade into a context-free provider lookup."""
    result = await research_linkedin_profile(email="buyer@customer.example")

    assert result["success"] is False
    assert "context" in result["error"]


@pytest.mark.asyncio
async def test_rate_limited_email_never_reaches_apollo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected paid-provider budget must stop before attendee data leaves the service."""
    context = _context(_Session())
    apollo = AsyncMock()
    monkeypatch.setattr(
        research_module,
        "get_internal_domains",
        AsyncMock(return_value={"seller.example"}),
    )
    monkeypatch.setattr(research_module, "apollo_enrich_person", apollo)
    monkeypatch.setattr(
        research_module,
        "enforce_contact_research_rate_limit",
        MagicMock(
            side_effect=HTTPException(
                status_code=429,
                detail="limit",
                headers={"Retry-After": "42"},
            )
        ),
    )

    result = await research_linkedin_profile(email="buyer@customer.example", context=context)

    assert result["success"] is False
    assert result["retry_after"] == "42"
    apollo.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_name_and_linkedin_lookalike_are_rejected() -> None:
    """Only exact email or a URL on LinkedIn's real hostname identifies a person."""
    plain_name = await research_linkedin_profile(query="Buyer Person")
    lookalike = await research_linkedin_profile(
        query="https://linkedin.com.evil.example/in/buyer"
    )

    assert plain_name["success"] is False
    assert lookalike["success"] is False


@pytest.mark.asyncio
async def test_valid_linkedin_url_returns_sanitized_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """General URL lookup remains available but never exposes Apollo's raw object."""
    apollo = AsyncMock(
        return_value={"success": True, "name": "Buyer", "raw": {"private": True}}
    )
    monkeypatch.setattr(research_module, "apollo_enrich_person", apollo)

    result = await research_linkedin_profile(query="linkedin.com/in/buyer")

    assert result["success"] is True
    assert result["profile"] == {"name": "Buyer"}
    apollo.assert_awaited_once_with(linkedin_url="https://linkedin.com/in/buyer")
