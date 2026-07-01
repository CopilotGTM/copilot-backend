"""Regression coverage for shared organisation internal-domain helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.utils import internal_domains


def test_normalize_internal_domains_strips_blanks_and_adds_www_alias() -> None:
    """Organisation domains are normalized once so every caller matches consistently."""
    assert internal_domains.normalize_internal_domains(
        [" Acme.com ", "www.Example.com", "", None]
    ) == {"acme.com", "www.example.com", "example.com"}


def test_is_internal_email_matches_normalized_domain_set() -> None:
    """Email checks should stay pure when the caller already loaded org domains."""
    domains = {"acme.com"}

    assert internal_domains.is_internal_email(" Buyer@Acme.COM ", domains)
    assert not internal_domains.is_internal_email("buyer@example.com", domains)
    assert not internal_domains.is_internal_email("not-an-email", domains)


@pytest.mark.asyncio
async def test_get_internal_domains_loads_organisation_domains_from_master_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain lookup must query master because Organisation is not in tenant schemas."""
    organisation_id = uuid4()

    class Result:
        """Minimal SQLAlchemy result stand-in for scalar domain payloads."""

        def scalar_one_or_none(self) -> list[str | None]:
            """Model the nullable list stored in the Organisation.domain column."""
            return [" Acme.com ", "www.Example.com", None]

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))

    async def fake_acquire_master_async_session():
        """Yield the mocked master session through the same async-generator contract."""
        yield session

    monkeypatch.setattr(
        internal_domains,
        "acquire_master_async_session",
        fake_acquire_master_async_session,
    )

    assert await internal_domains.get_internal_domains(organisation_id) == {
        "acme.com",
        "www.example.com",
        "example.com",
    }
    session.execute.assert_awaited_once()
